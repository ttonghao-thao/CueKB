from datetime import datetime
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, SecretStr, model_validator

from cuekb.domain.models import (
    Applicability,
    ContextLink,
    ContextProvenance,
    ContextRole,
    DependencyIssue,
    EvidenceStatus,
    RetrievalMode,
    RetrievalStatus,
    SourceAnchor,
    SourceSpan,
    VersionStatus,
)


class KnowledgeBaseCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    name: str = Field(
        min_length=1, max_length=200,
        description="Display name; not unique and not a retrieval identifier.",
    )
    description: str = Field(default="", max_length=2000)


class ModelConfigurationUpdate(BaseModel):
    embedding_base_url: str = Field(min_length=1, max_length=2000)
    embedding_api_key: SecretStr | None = Field(default=None, max_length=2000)
    embedding_model: str = Field(min_length=1, max_length=500)
    reranker_base_url: str = Field(default="", max_length=2000)
    reranker_api_key: SecretStr | None = Field(default=None, max_length=2000)
    reranker_model: str = Field(default="", max_length=500)

    @model_validator(mode="after")
    def validate_services(self) -> "ModelConfigurationUpdate":
        self.embedding_base_url = self.embedding_base_url.strip()
        self.embedding_model = self.embedding_model.strip()
        self.reranker_base_url = self.reranker_base_url.strip()
        self.reranker_model = self.reranker_model.strip()
        for address in (self.embedding_base_url, self.reranker_base_url):
            if not address:
                continue
            parsed = urlsplit(address)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.netloc
                or parsed.path != "/v1"
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError("model_base_url_must_be_http_url_ending_in_v1")
        if not self.embedding_base_url or not self.embedding_model:
            raise ValueError("embedding_service_required")
        if (
            self.embedding_api_key is not None and not self.embedding_api_key.get_secret_value()
        ) or (self.reranker_api_key is not None and not self.reranker_api_key.get_secret_value()):
            raise ValueError("empty_api_key_not_allowed")
        if self.reranker_base_url and not self.reranker_model:
            raise ValueError("reranker_model_required")
        if not self.reranker_base_url and self.reranker_model:
            raise ValueError("reranker_url_required")
        return self


class ModelConfigurationStatus(BaseModel):
    configured: bool
    embedding_base_url: str = ""
    embedding_model: str = ""
    embedding_api_key_set: bool = False
    reranker_base_url: str = ""
    reranker_model: str = ""
    reranker_api_key_set: bool = False
    revision: int | None = None
    updated_at: datetime | None = None


class SearchFilters(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_ids: list[UUID] = Field(default_factory=list)
    product_model: str | None = None
    product_entity_id: UUID | None = None
    product_entity_ids: list[UUID] = Field(default_factory=list, max_length=20)
    software_version: str | None = None
    category: str | None = None

    @model_validator(mode="after")
    def valid_product_entities(self):
        if self.product_entity_id is not None and self.product_entity_ids:
            raise ValueError("product_entity_filters_are_mutually_exclusive")
        if len(set(self.product_entity_ids)) != len(self.product_entity_ids):
            raise ValueError("product_entity_ids_must_not_contain_duplicates")
        return self


class HealthResponse(BaseModel):
    status: str
    version: str


class PublishRequest(BaseModel):
    version_id: UUID
    scope_key: Literal["default"] = "default"


class ApiKeyCreate(BaseModel):
    principal_name: str = Field(min_length=1, max_length=200)
    label: str = Field(min_length=1, max_length=200)


class ApiKeyCreated(BaseModel):
    id: UUID
    principal_id: UUID
    api_key: str


class GrantCreate(BaseModel):
    principal_id: UUID
    role: str = Field(pattern="^(read|write|admin)$")


class KnowledgeBaseAccess(BaseModel):
    id: UUID
    name: str
    description: str
    content_revision: int
    acl_revision: int
    role: Literal["read", "write", "admin"]
    created_at: datetime


class DocumentSummary(BaseModel):
    id: UUID
    kb_id: UUID
    name: str
    created_at: datetime
    version_count: int
    latest_version_id: UUID | None = None
    latest_version_status: VersionStatus | None = None
    active_version_id: UUID | None = None
    active_business_version: str | None = None


RelationType = Literal[
    "belongs_to",
    "adjacent_to",
    "alias_of",
    "revises",
    "replaces",
    "references",
    "depends_on",
    "applies_to",
]


class EntityWrite(BaseModel):
    name: str = Field(min_length=1, max_length=200, pattern=r"\S")
    kind: str = Field(default="term", min_length=1, max_length=100)
    product_model: str | None = Field(default=None, min_length=1, max_length=200)
    software_version: str | None = Field(default=None, min_length=1, max_length=200)
    aliases: list[str] = Field(default_factory=list, max_length=50)
    mention_chunk_ids: list[UUID] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def validate_aliases(self) -> "EntityWrite":
        self.name = self.name.strip()
        if any(not a.strip() or len(a) > 200 for a in self.aliases):
            raise ValueError("invalid_alias")
        self.aliases = sorted({a.strip() for a in self.aliases})
        return self


class RelationEvidenceWrite(BaseModel):
    chunk_id: UUID
    stance: Literal["supports", "refutes"] = "supports"


class RelationWrite(BaseModel):
    subject_id: UUID
    object_id: UUID
    relation_type: RelationType
    evidence: list[RelationEvidenceWrite] = Field(min_length=1, max_length=50)
    conditions: dict[Literal["product_model", "software_version"], str] = Field(
        default_factory=dict
    )
    valid_from: datetime | None = None
    valid_until: datetime | None = None

    @model_validator(mode="after")
    def validate_interval(self) -> "RelationWrite":
        if self.subject_id == self.object_id:
            raise ValueError("self_relation_not_allowed")
        for value in (self.valid_from, self.valid_until):
            if value is not None and value.tzinfo is None:
                raise ValueError("relation_time_requires_timezone")
        if self.valid_from and self.valid_until and self.valid_until <= self.valid_from:
            raise ValueError("invalid_relation_interval")
        return self


class RelationQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entity_ids: list[UUID] = Field(default_factory=list, max_length=20)
    types: list[RelationType] = Field(default_factory=list, max_length=8)
    direction: Literal["outgoing", "incoming", "both"] = "outgoing"
    at: datetime | None = None

    @model_validator(mode="after")
    def aware_time(self) -> "RelationQuery":
        if self.at is not None and self.at.tzinfo is None:
            raise ValueError("relation_time_requires_timezone")
        return self


class GenerationCreate(ModelConfigurationUpdate):
    dimension: int = Field(ge=1, le=65536)
    chunking_version: str | None = Field(default=None, min_length=1, max_length=100)


class V2SearchFilters(SearchFilters):
    document_ids: list[UUID] = Field(default_factory=list, max_length=100)


class V2SearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=2000)
    kb_ids: list[UUID] = Field(min_length=1, max_length=50)
    request_id: str | None = Field(default=None, min_length=1, max_length=128)
    mode: RetrievalMode = RetrievalMode.AUTO
    top_k: int = Field(default=8, ge=1, le=20)
    include_context: bool = True
    relations: RelationQuery = Field(default_factory=RelationQuery)
    filters: V2SearchFilters = Field(default_factory=V2SearchFilters)
    max_evidence_bytes: int = Field(default=48000, ge=4096, le=128000)
    include_diagnostics: bool = False
    selection_mode: Literal["ranked", "adaptive"] = "ranked"
    selection_query: str | None = Field(default=None, min_length=1, max_length=2000)
    query_facets: list[Annotated[str, Field(min_length=1, max_length=200)]] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def unique_kbs(self) -> "V2SearchRequest":
        if len(set(self.kb_ids)) != len(self.kb_ids):
            raise ValueError("kb_ids must not contain duplicates")
        return self

    @model_validator(mode="after")
    def adaptive_inputs(self):
        if self.selection_mode != "adaptive" and (self.selection_query is not None or self.query_facets):
            raise ValueError("selection_inputs_require_adaptive_mode")
        if any(not facet.strip() for facet in self.query_facets) or (self.selection_query is not None and not self.selection_query.strip()):
            raise ValueError("selection_inputs_must_not_be_blank")
        return self


ContextOmission = Literal[
    "budget_omitted", "candidate_limit_reached", "dependency_unresolved", "unsafe_scope_excluded"
]
RankingMethod = Literal["keyword_rrf", "rrf", "rrf_related", "rrf_then_rerank"]


class V2ContextPart(BaseModel):
    model_config = ConfigDict(extra="forbid")

    chunk_id: UUID
    source_text: str
    anchor: SourceAnchor
    title_path: list[str]

    source_spans: list[SourceSpan] = Field(default_factory=list, max_length=64)
    role: ContextRole | None = None
    required: bool = False
    applicability: Applicability = Field(default_factory=Applicability)
    link_provenance: ContextProvenance | None = None


class V2RelationEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    relation_id: UUID
    subject_id: UUID
    object_id: UUID
    relation_type: RelationType
    conditions: dict[str, Any] = Field(default_factory=dict)
    stance: Literal["supports", "refutes"]
    chunk_id: UUID


class V2SearchHit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    chunk_id: UUID
    document_id: UUID
    version_id: UUID
    source_text: str
    context: str | None = None
    title_path: list[str]
    anchor: SourceAnchor
    metadata: dict[str, Any]
    retrieval_sources: list[str]
    document_name: str | None = None
    content_sha256: str | None = None
    context_truncated: bool = False

    rank: int = Field(ge=1, le=20)
    score: FiniteFloat
    score_type: Literal["rrf", "rerank"]
    stage_scores: dict[str, FiniteFloat] = Field(default_factory=dict)
    applicability: Applicability = Field(default_factory=Applicability)
    fact_kind: Literal["statement", "specification", "comparison", "bundle", "general"] = "statement"
    dependency_issues: list[DependencyIssue] = Field(default_factory=list, max_length=5)
    source_spans: list[SourceSpan] = Field(default_factory=list, max_length=64)
    processing_profile: str
    source_unit_id: str | None = Field(default=None, max_length=200)
    block_kind: Literal["text", "list", "table", "heading"] = "text"
    context_parts: list[V2ContextPart] = Field(default_factory=list, max_length=21)
    context_state: Literal["disabled", "complete", "truncated", "unresolved"]
    context_omissions: list[ContextOmission] = Field(default_factory=list, max_length=4)
    relations: list[V2RelationEvidence] = Field(default_factory=list, max_length=50)


class V2CandidateCounts(BaseModel):
    model_config = ConfigDict(extra="forbid")

    keyword: int = Field(default=0, ge=0, le=100)
    vector: int = Field(default=0, ge=0, le=100)
    relation: int = Field(default=0, ge=0, le=50)
    union: int = Field(default=0, ge=0, le=250)
    authoritative: int = Field(default=0, ge=0, le=250)
    duplicate_removed: int = Field(default=0, ge=0, le=250)
    reranked: int = Field(default=0, ge=0, le=100)
    returned: int = Field(default=0, ge=0, le=20)


class V2OmissionCounts(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope_excluded: int = Field(default=0, ge=0)
    dependency_unresolved: int = Field(default=0, ge=0)
    candidate_limit: int = Field(default=0, ge=0)
    rerank_window_limit: int = Field(default=0, ge=0)
    payload_budget_exhausted: int = Field(default=0, ge=0)
    stale_candidate: int = Field(default=0, ge=0)
    heading_only: int = Field(default=0, ge=0)
    context_budget_omitted: int = Field(default=0, ge=0)


class V2PayloadSizes(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_text_chars: int = Field(default=0, ge=0)
    source_text_bytes: int = Field(default=0, ge=0)
    context_chars: int = Field(default=0, ge=0)
    context_bytes: int = Field(default=0, ge=0)
    context_parts_chars: int = Field(default=0, ge=0)
    context_parts_bytes: int = Field(default=0, ge=0)
    hits_bytes: int = Field(default=2, ge=2)


class V2BackendCalls(BaseModel):
    model_config = ConfigDict(extra="forbid")

    keyword: int = Field(default=0, ge=0)
    embedding: int = Field(default=0, ge=0)
    vector: int = Field(default=0, ge=0)
    relation: int = Field(default=0, ge=0)
    rerank: int = Field(default=0, ge=0)


class V2RerankerDiagnostics(BaseModel):
    model_config = ConfigDict(extra="forbid")

    configured: bool
    executed: bool
    skipped_reason: Literal[
        "exact_path", "reranker_service_unconfigured", "rerank_disabled",
        "model_client_unconfigured", "not_enough_candidates", "insufficient_remaining_budget"
    ] | None = None


class V2SearchDiagnostics(BaseModel):
    model_config = ConfigDict(extra="forbid")

    counts: V2CandidateCounts = Field(default_factory=V2CandidateCounts)
    omissions: V2OmissionCounts = Field(default_factory=V2OmissionCounts)
    sizes: V2PayloadSizes = Field(default_factory=V2PayloadSizes)
    backend_calls: V2BackendCalls = Field(default_factory=V2BackendCalls)
    attempt_count: int = Field(default=1, ge=1, le=2)
    reranker: V2RerankerDiagnostics
    active_generation: str | None = None
    processing_profiles: list[str] = Field(default_factory=list, max_length=250)
    algorithm_revision: Literal["evidence-ranking-v2.1"] = "evidence-ranking-v2.1"
    keyword_limit: int = Field(ge=1, le=100)
    vector_limit: int = Field(ge=0, le=100)
    relation_limit: int = Field(ge=0, le=50)
    selection: "V2SelectionDiagnostics | None" = None


class V2SelectionDiagnostics(BaseModel):
    model_config = ConfigDict(extra="forbid")
    method: Literal["adaptive"] = "adaptive"
    version: Literal["evidence-set-v1"] = "evidence-set-v1"
    calibration_status: Literal["not_configured", "applied", "binding_mismatch"]
    profile_id: str | None = Field(default=None, pattern=r"^sha256:[a-f0-9]{64}$")
    stop_reason: Literal["no_candidates", "relevance_exhausted", "no_information_gain", "max_hits", "payload_budget", "candidates_exhausted"]
    considered: int = Field(default=0, ge=0, le=250)
    assessed: int = Field(default=0, ge=0, le=250)
    rejected: int = Field(default=0, ge=0, le=250)
    redundant: int = Field(default=0, ge=0, le=250)
    facet_count: int = Field(default=0, ge=0, le=8)
    observed_facet_indices: list[Annotated[int, Field(ge=0, le=7)]] = Field(default_factory=list, max_length=8)
    coverage_assessment: Literal["unassessed"] = "unassessed"


class V2SearchResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    trace_id: UUID
    request_id: str | None = None
    retrieval_status: RetrievalStatus
    evidence_status: EvidenceStatus = EvidenceStatus.UNASSESSED
    degraded_reasons: list[str] = Field(default_factory=list)
    scope_limited: bool = False
    content_revisions: dict[str, int]
    timings_ms: dict[str, float]
    retrieval_path: str = "keyword"
    executed_stages: list[str] = Field(default_factory=list)
    skipped_stages: list[dict[str, str]] = Field(default_factory=list)

    schema_version: Literal["2"] = "2"
    ranking_method: RankingMethod
    ranking_version: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    diagnostics: V2SearchDiagnostics
    hits: list[V2SearchHit] = Field(max_length=20)


EVIDENCE_CONTRACT_REVISION = "cuekb.evidence/2.1"


class EvidenceFacetInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")
    text: str = Field(min_length=1, max_length=500)
    question_span: list[Annotated[int, Field(ge=0, le=2000)]] = Field(
        min_length=2, max_length=2
    )

    @model_validator(mode="after")
    def valid_span(self):
        if self.question_span[0] >= self.question_span[1]:
            raise ValueError("facet_question_span_invalid")
        if not self.text.strip():
            raise ValueError("facet_text_must_not_be_blank")
        return self


class EvidenceContextBinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entity_id: UUID
    source: Literal["confirmed_conversation_context", "user_selection"]


class EvidenceQueryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contract_revision: Literal["cuekb.evidence/2.1"] = EVIDENCE_CONTRACT_REVISION
    question: str = Field(min_length=1, max_length=2000)
    search_query: str | None = Field(default=None, min_length=1, max_length=2000)
    kb_ids: list[UUID] = Field(min_length=1, max_length=50)
    filters: V2SearchFilters = Field(default_factory=V2SearchFilters)
    context_bindings: list[EvidenceContextBinding] = Field(default_factory=list, max_length=8)
    requested_facets: list[EvidenceFacetInput] = Field(default_factory=list, max_length=8)
    delivery_profile: Literal["standard", "voice"] = "standard"
    max_groups: int = Field(default=5, ge=1, le=5)
    token_budget: int = Field(default=120, ge=32, le=4096)
    timeout_ms: int | None = Field(default=None, ge=100, le=10000)
    request_id: str | None = Field(default=None, min_length=1, max_length=128)
    input_revision: int = Field(default=1, ge=1, le=2147483647)

    @model_validator(mode="after")
    def validate_query(self):
        if not self.question.strip() or (
            self.search_query is not None and not self.search_query.strip()
        ):
            raise ValueError("evidence_query_must_not_be_blank")
        if len(set(self.kb_ids)) != len(self.kb_ids):
            raise ValueError("kb_ids_must_not_contain_duplicates")
        facet_ids = [facet.id for facet in self.requested_facets]
        if len(set(facet_ids)) != len(facet_ids):
            raise ValueError("facet_ids_must_not_contain_duplicates")
        for facet in self.requested_facets:
            start, end = facet.question_span
            if end > len(self.question) or self.question[start:end] != facet.text:
                raise ValueError("facet_text_must_match_question_span")
        entities = [binding.entity_id for binding in self.context_bindings]
        if len(set(entities)) != len(entities):
            raise ValueError("context_bindings_must_not_contain_duplicates")
        if self.filters.product_entity_id is not None and entities and (
            len(entities) != 1 or entities[0] != self.filters.product_entity_id
        ):
            raise ValueError("context_binding_conflicts_with_product_filter")
        if self.filters.product_entity_ids and entities and (
            set(entities) != set(self.filters.product_entity_ids)
        ):
            raise ValueError("context_binding_conflicts_with_product_filter")
        return self


EvidenceSupport = Literal[
    "direct", "entailed", "absent", "ambiguous", "conflicting", "unassessed"
]


class EvidenceAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    method_revision: Literal["structured-support-1"] = "structured-support-1"
    calibration_id: str | None = None
    scope: Literal["authorized_sources_for_this_question"] = (
        "authorized_sources_for_this_question"
    )
    limitations: list[str] = Field(default_factory=list, max_length=20)


class EvidenceFacetResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    text: str
    support: EvidenceSupport
    evidence_ids: list[str] = Field(default_factory=list, max_length=5)


class EvidenceSubject(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entity_id: UUID | None = None
    name: str | None = Field(default=None, max_length=200)
    resolution: Literal["known", "general", "ambiguous", "unknown"]


class EvidenceStatement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=16000)
    source_refs: list[str] = Field(min_length=1, max_length=21)


class EvidenceGroup(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^E[1-5]$")
    priority: Literal["primary", "supporting"]
    rank: int = Field(ge=1, le=5)
    supports: list[str] = Field(min_length=1, max_length=8)
    subject: EvidenceSubject
    statement: EvidenceStatement
    required_context: list[str] = Field(default_factory=list, max_length=20)
    conditions: list[str] = Field(default_factory=list, max_length=20)
    completeness: Literal["complete", "incomplete"]
    match_basis: list[
        Literal[
            "confirmed_subject", "explicit_subject", "general_scope",
            "explicit_attribute", "exact_value", "necessary_context",
        ]
    ] = Field(min_length=1, max_length=6)


class EvidenceSourceRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    chunk_id: UUID
    document_id: UUID
    version_id: UUID
    node_id: UUID
    anchor: SourceAnchor


class EvidenceConflict(BaseModel):
    model_config = ConfigDict(extra="forbid")

    facet_id: str
    evidence_ids: list[str] = Field(min_length=2, max_length=5)
    reason: Literal["incompatible_values", "opposite_polarity"]


class EvidenceClarificationOption(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entity_id: UUID
    label: str = Field(min_length=1, max_length=200)


class EvidenceClarification(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=500)
    options: list[EvidenceClarificationOption] = Field(min_length=2, max_length=8)


class EvidenceContinuation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    allowed: bool = False
    reason: str = Field(min_length=1, max_length=100)
    token: str | None = Field(default=None, min_length=20, max_length=16384)

    @model_validator(mode="after")
    def token_matches_permission(self):
        if self.allowed != (self.token is not None):
            raise ValueError("continuation_token_must_match_allowed")
        return self


class EvidenceDelivery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile: Literal["standard", "voice"]
    serialization: Literal["canonical-json-v1"] = "canonical-json-v1"
    tokenizer_id: str = Field(min_length=1, max_length=200)
    byte_count: int = Field(ge=2, le=131072)
    token_count: int = Field(ge=1, le=4096)
    token_budget: int = Field(ge=32, le=4096)


class EvidenceModelItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    for_: str = Field(alias="for", serialization_alias="for", min_length=1, max_length=4000)
    text: str = Field(min_length=1, max_length=16000)
    conditions: list[str] = Field(default_factory=list, max_length=40)


class EvidenceModelView(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    decision: Literal["answer", "partial_answer", "clarify", "insufficient", "conflict"]
    evidence: list[EvidenceModelItem] = Field(default_factory=list, max_length=5)
    missing: list[str] = Field(default_factory=list, max_length=8)
    search: Literal["finished"] = "finished"


class EvidenceRetrievalSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: RetrievalStatus
    resolved_entity_ids: list[UUID] = Field(default_factory=list, max_length=8)
    candidate_groups: int = Field(ge=0, le=20)
    selected_groups: int = Field(ge=0, le=5)
    snapshot_id: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")


class EvidencePackage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=20, max_length=16384)
    expires_at: datetime


class EvidenceQueryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contract_revision: Literal["cuekb.evidence/2.1"] = EVIDENCE_CONTRACT_REVISION
    trace_id: UUID
    request_id: str | None = None
    input_revision: int = Field(ge=1)
    decision: Literal["answer", "partial_answer", "clarify", "insufficient", "conflict"]
    assessment: EvidenceAssessment
    facets: list[EvidenceFacetResult] = Field(min_length=1, max_length=8)
    evidence: list[EvidenceGroup] = Field(default_factory=list, max_length=5)
    missing: list[str] = Field(default_factory=list, max_length=8)
    conflicts: list[EvidenceConflict] = Field(default_factory=list, max_length=8)
    clarification: EvidenceClarification | None = None
    continuation: EvidenceContinuation
    sources: dict[str, EvidenceSourceRef] = Field(default_factory=dict, max_length=105)
    delivery: EvidenceDelivery
    model_view: EvidenceModelView
    retrieval: EvidenceRetrievalSummary
    content_revisions: dict[str, int]
    authorization_revisions: dict[str, int]
    timings_ms: dict[str, float]
    package: EvidencePackage

    @model_validator(mode="after")
    def consistent_evidence(self):
        facet_ids = {facet.id for facet in self.facets}
        groups = {group.id: group for group in self.evidence}
        if len(groups) != len(self.evidence):
            raise ValueError("evidence_group_ids_must_be_unique")
        if any(
            support not in groups
            for facet in self.facets
            for support in facet.evidence_ids
        ):
            raise ValueError("facet_evidence_reference_missing")
        if any(
            facet.support in {"direct", "entailed"} and not facet.evidence_ids
            for facet in self.facets
        ):
            raise ValueError("supported_facet_requires_evidence")
        if any(
            facet.support not in {"direct", "entailed", "ambiguous", "conflicting"}
            and facet.evidence_ids
            for facet in self.facets
        ):
            raise ValueError("unsupported_facet_must_not_reference_evidence")
        if any(
            facet not in facet_ids or source not in self.sources
            for group in self.evidence
            for facet in group.supports
            for source in group.statement.source_refs
        ):
            raise ValueError("evidence_group_reference_missing")
        if any(
            set(facet.evidence_ids) != {
                group.id for group in self.evidence if facet.id in group.supports
            }
            for facet in self.facets
        ):
            raise ValueError("facet_and_group_support_must_match")
        referenced_sources = {
            source
            for group in self.evidence
            for source in group.statement.source_refs
        }
        if referenced_sources != set(self.sources):
            raise ValueError("sources_must_match_selected_evidence")
        if self.decision == "answer" and any(
            facet.support not in {"direct", "entailed"} for facet in self.facets
        ):
            raise ValueError("answer_requires_all_facets_supported")
        if self.model_view.decision != self.decision or self.model_view.missing != self.missing:
            raise ValueError("model_view_must_match_decision")
        model_items = {item.id: item for item in self.model_view.evidence}
        if (
            len(model_items) != len(self.model_view.evidence)
            or set(model_items) != set(groups)
            or any(
                model_items[group_id].text != group.statement.text
                or model_items[group_id].conditions != list(dict.fromkeys([
                    *group.conditions, *group.required_context,
                ]))
                for group_id, group in groups.items()
            )
        ):
            raise ValueError("model_view_evidence_must_match_selected_groups")
        return self


class EvidenceValidateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contract_revision: Literal["cuekb.evidence/2.1"] = EVIDENCE_CONTRACT_REVISION
    package_id: str = Field(min_length=20, max_length=16384)
    request_id: str | None = Field(default=None, min_length=1, max_length=128)


class EvidenceValidateResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contract_revision: Literal["cuekb.evidence/2.1"] = EVIDENCE_CONTRACT_REVISION
    trace_id: UUID
    request_id: str | None = None
    status: Literal["valid", "stale"]
    content_revisions: dict[str, int]
    authorization_revisions: dict[str, int]
    validated_at: datetime


class CuratedSubjectInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entity_id: UUID | None = None
    display_name: str = Field(min_length=1, max_length=200)
    product_model: str | None = Field(default=None, min_length=1, max_length=200)
    software_version: str | None = Field(default=None, min_length=1, max_length=200)
    provenance_id: str | None = Field(default=None, min_length=1, max_length=500)

    @model_validator(mode="after")
    def meaningful_subject(self):
        if any(value is not None and not value.strip() for value in (
            self.display_name, self.product_model, self.software_version, self.provenance_id)):
            raise ValueError("scope_subject_must_not_be_blank")
        return self


class CuratedConditionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=2000)
    provenance_id: str | None = Field(default=None, min_length=1, max_length=500)


class CuratedScopeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subjects: list[CuratedSubjectInput] = Field(min_length=1, max_length=8)
    conditions: list[CuratedConditionInput] = Field(default_factory=list, max_length=20)


class ScopeBindingInput(CuratedScopeInput):
    title_path: list[str] | None = Field(default=None, min_length=1, max_length=20)
    source_unit_id: str | None = Field(default=None, min_length=1, max_length=200)

    @model_validator(mode="after")
    def require_selector(self) -> "ScopeBindingInput":
        if self.title_path is None and self.source_unit_id is None:
            raise ValueError("scope_binding_requires_selector")
        if self.title_path is not None and any(not part.strip() for part in self.title_path):
            raise ValueError("scope_binding_title_must_not_be_blank")
        return self


class SectionSelectorInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title_path: list[Annotated[str, Field(min_length=1, max_length=500)]] | None = Field(
        default=None, min_length=1, max_length=20)
    source_unit_id: str | None = Field(default=None, min_length=1, max_length=200)

    @model_validator(mode="after")
    def require_selector(self):
        if self.title_path is None and self.source_unit_id is None:
            raise ValueError("section_selector_required")
        if self.title_path and any(not title.strip() for title in self.title_path):
            raise ValueError("section_title_must_not_be_blank")
        return self


class SectionDependencyInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: SectionSelectorInput
    target: SectionSelectorInput
    role: ContextRole
    provenance_id: str = Field(min_length=1, max_length=500)
    reference_text: str | None = Field(default=None, min_length=1, max_length=500)

    @model_validator(mode="after")
    def meaningful_provenance(self):
        if not self.provenance_id.strip() or (self.reference_text is not None and not self.reference_text.strip()):
            raise ValueError("dependency_provenance_must_not_be_blank")
        return self


class SourceMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")
    language: str = Field(default="und", min_length=2, max_length=35)
    source_owner: str | None = Field(default=None, max_length=200)
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    authority_policy_ref: str | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def valid_interval(self):
        for value in (self.valid_from, self.valid_to):
            if value is not None and value.tzinfo is None: raise ValueError("source_time_requires_timezone")
        if self.valid_from and self.valid_to and self.valid_to <= self.valid_from:
            raise ValueError("invalid_source_validity_interval")
        return self


class ReprocessRequest(BaseModel):
    content_kind: Literal["auto", "general", "product"] | None = None
    source_metadata: SourceMetadata | None = None
    model_config = ConfigDict(extra="forbid")

    source_version_id: UUID | None = None
    target_profile: Literal["evidence-v2"] = "evidence-v2"
    scope_bindings: list[ScopeBindingInput] = Field(default_factory=list, max_length=100)
    document_default: CuratedScopeInput | None = None
    dependency_bindings: list[SectionDependencyInput] = Field(default_factory=list, max_length=100)
    auto_publish: bool = False
    text_mode: Literal["auto", "plain"] = "auto"
    processing_template_id: UUID | None = None


class RestoreRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class V2DocumentMetadata(BaseModel):
    content_kind: Literal["auto", "general", "product"] = "auto"
    source_metadata: SourceMetadata = Field(default_factory=SourceMetadata)
    model_config = ConfigDict(extra="forbid")

    kb_id: UUID
    name: str = Field(min_length=1, max_length=500)
    business_version: str | None = Field(default=None, max_length=100)
    scope: dict[str, Any] = Field(default_factory=dict)
    document_id: UUID | None = None
    auto_publish: bool = False
    text_mode: Literal["auto", "plain"] = "auto"
    processing_template_id: UUID | None = None

    scope_bindings: list[ScopeBindingInput] = Field(default_factory=list, max_length=100)
    document_default: CuratedScopeInput | None = None
    dependency_bindings: list[SectionDependencyInput] = Field(default_factory=list, max_length=100)


class V2DocumentVersionInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    content_sha256: str
    business_version: str | None = None
    scope: dict[str, Any]
    status: VersionStatus
    original_filename: str | None = None
    media_type: str | None = None
    created_at: datetime
    is_active: bool = False
    processing_profile: str
    derivation_kind: Literal["ingest", "reprocess", "restore"]
    source_version_id: UUID | None = None


class V2DocumentDetail(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    kb_id: UUID
    name: str
    created_at: datetime
    active_version_id: UUID | None = None
    versions: list[V2DocumentVersionInfo]


class ProcessingReviewItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    chunk_id: UUID
    source_unit_id: str | None
    title_path: list[str]
    source_preview: str = Field(max_length=500)
    applicability: Applicability
    context_links: list[ContextLink] = Field(max_length=20)
    dependency_unresolved: bool
    dependency_issues: list[str] = Field(max_length=5)


class ProcessingReview(BaseModel):
    content_kind: Literal["auto", "general", "product"] = "auto"
    model_config = ConfigDict(extra="forbid")
    document_id: UUID
    kb_id: UUID
    version_id: UUID
    processing_profile: str
    status: VersionStatus
    requires_reprocess: bool
    total: int = Field(ge=0)
    resolved: int = Field(ge=0)
    unknown: int = Field(ge=0)
    ambiguous: int = Field(ge=0)
    unresolved_dependencies: int = Field(ge=0)
    reason_counts: dict[str, int]
    warnings: list[str] = Field(max_length=5)
    scope_bindings: list[ScopeBindingInput]
    document_default: CuratedScopeInput | None
    dependency_bindings: list[SectionDependencyInput]
    offset: int = Field(ge=0)
    limit: int = Field(ge=1, le=100)
    items: list[ProcessingReviewItem] = Field(max_length=100)
    publish_eligible: bool = False
    blocking_facts: int = Field(default=0, ge=0)
    review_plan_id: UUID | None = None


class ReviewPlanCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ReviewChoice(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entity_id: UUID
    display_name: str
    product_model: str | None = None
    software_version: str | None = None
    kind: str = "product"


class ReviewFact(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: UUID
    source_chunk_id: UUID
    source_text: str
    title_path: list[str]
    anchor: SourceAnchor
    applicability: Applicability
    fact_kind: str
    review_state: str
    reason: str
    category: str
    recommended_action: str
    allowed_actions: list[str]
    guidance: str
    candidates: list[ReviewChoice] = Field(max_length=5)
    dependencies: list[UUID] = Field(default_factory=list, max_length=20)
    dependency_candidates: list[UUID] = Field(default_factory=list, max_length=5)
    dependency_choices: list[dict[str, Any]] = Field(default_factory=list, max_length=5)
    dependency_issues: list[str] = Field(default_factory=list, max_length=5)
    # Character ranges refer to this exact source_text; values are never editable.
    split_suggestions: list[tuple[int, int]] = Field(default_factory=list, max_length=50)


class ReviewPlanInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: UUID
    document_id: UUID
    version_id: UUID
    kb_id: UUID
    revision: int
    status: Literal["draft", "applied"]
    total: int
    counts: dict[str, int]
    blocking: int
    publish_eligible: bool
    rule_revision: str
    applied_job_id: UUID | None = None
    reason_counts: dict[str, int] = Field(default_factory=dict)
    sections: list[dict[str, str]] = Field(default_factory=list, max_length=1000)
    sections_truncated: bool = False
    version_status: str = "ready"
    editable: bool = True


class ReviewFactPage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int
    total: int
    offset: int
    limit: int
    items: list[ReviewFact] = Field(max_length=100)


class ReviewDecisionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fact_id: UUID
    action: Literal["accept", "assign", "general", "defer", "exclude", "reset", "split", "dependency", "comparison", "shared"]
    entity_id: UUID | None = None
    entity_ids: list[UUID] = Field(default_factory=list, max_length=8)
    ranges: list[tuple[int, int]] = Field(default_factory=list, max_length=50)
    target_fact_id: UUID | None = None
    role: ContextRole = "condition"

    @model_validator(mode="after")
    def validate_product_choices(self):
        if len(set(self.entity_ids)) != len(self.entity_ids):
            raise ValueError("duplicate_product_choice")
        if self.entity_ids and (self.action not in {"shared", "comparison"} or self.entity_id is not None):
            raise ValueError("product_choices_do_not_match_action")
        return self


class ReviewDecisionsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=0)
    decisions: list[ReviewDecisionInput] = Field(min_length=1, max_length=100)


class ReviewApplyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=0)


class ReviewSelectionCreate(ReviewApplyRequest):
    issues: bool = True
    reason: str | None = Field(default=None, max_length=200)
    section_id: UUID | None = None
    product_model: str | None = Field(default=None, max_length=200)


class ReviewSelectionApply(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["general", "exclude", "defer", "assign"]
    entity_id: UUID | None = None


class ReviewSelectionInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: UUID
    plan_id: UUID
    count: int
    completed: int
    revision: int
    digest: str
    operation: dict[str, Any] | None = None


class EntityImportRow(EntityWrite):
    id: UUID


class EntityImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rows: list[EntityImportRow] = Field(min_length=1, max_length=100)


class ProcessingTemplateWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=200)
    general_headings: list[Annotated[str, Field(min_length=1, max_length=200)]] = Field(default_factory=list, max_length=100)
    attribute_terms: list[Annotated[str, Field(min_length=1, max_length=100)]] = Field(default_factory=list, max_length=100)
    text_mode: Literal["auto", "plain"] = "auto"


class ProcessingTemplateInfo(ProcessingTemplateWrite):
    id: UUID
    revision: int


class EntityInfo(EntityWrite):
    id: UUID
    kb_id: UUID


class RelationInfo(RelationWrite):
    id: UUID
    kb_id: UUID


class GenerationQueued(BaseModel):
    id: UUID
    status: Literal["queued"] = "queued"


class GenerationInfo(BaseModel):
    id: UUID
    index_name: str
    embedding_model: str
    dimension: int
    chunking_version: str
    status: Literal["queued", "building", "ready", "active", "retired", "failed"]
    source_revision: int
    chunk_count: int | None
    attempt_count: int
    error_code: str | None
    created_at: datetime
    updated_at: datetime
    build_source_profiles: dict[str, int] = Field(default_factory=dict)
