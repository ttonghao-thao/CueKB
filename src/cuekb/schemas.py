from datetime import datetime
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

from cuekb.domain.models import (
    EvidenceStatus,
    RetrievalMode,
    RetrievalStatus,
    SourceAnchor,
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


class DocumentCreate(BaseModel):
    kb_id: UUID
    name: str = Field(min_length=1, max_length=500)
    content: str = Field(min_length=1)
    business_version: str | None = Field(default=None, max_length=100)
    scope: dict[str, Any] = Field(default_factory=dict)


class SearchFilters(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_ids: list[UUID] = Field(default_factory=list)
    product_model: str | None = None
    software_version: str | None = None
    category: str | None = None


class SearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=2000)
    kb_ids: list[UUID] = Field(min_length=1, max_length=50)
    request_id: str | None = Field(default=None, min_length=1, max_length=128)
    mode: RetrievalMode = RetrievalMode.AUTO
    top_k: int = Field(default=8, ge=1, le=20)
    filters: SearchFilters = Field(default_factory=SearchFilters)
    include_context: bool = True
    relations: "RelationQuery" = Field(default_factory=lambda: RelationQuery())

    @model_validator(mode="after")
    def unique_kbs(self) -> "SearchRequest":
        if len(set(self.kb_ids)) != len(self.kb_ids):
            raise ValueError("kb_ids must not contain duplicates")
        return self


class SearchHit(BaseModel):
    chunk_id: UUID
    document_id: UUID
    version_id: UUID
    rank: int
    source_text: str
    context: str | None = None
    title_path: list[str]
    anchor: SourceAnchor
    metadata: dict[str, Any]
    retrieval_sources: list[str]
    document_name: str | None = None
    content_sha256: str | None = None
    score: float | None = None
    score_type: str | None = None
    stage_scores: dict[str, float] = Field(default_factory=dict)
    context_parts: list["ContextPart"] = Field(default_factory=list)
    context_truncated: bool = False
    relations: list[dict[str, Any]] = Field(default_factory=list)


class SearchResponse(BaseModel):
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
    hits: list[SearchHit]


class HealthResponse(BaseModel):
    status: str
    version: str


class PublishRequest(BaseModel):
    version_id: UUID
    scope_key: Literal["default"] = "default"


class DocumentMetadata(BaseModel):
    kb_id: UUID
    name: str = Field(min_length=1, max_length=500)
    business_version: str | None = Field(default=None, max_length=100)
    scope: dict[str, Any] = Field(default_factory=dict)
    document_id: UUID | None = None
    auto_publish: bool = False


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


class DocumentVersionInfo(BaseModel):
    id: UUID
    content_sha256: str
    business_version: str | None = None
    scope: dict[str, Any]
    status: VersionStatus
    original_filename: str | None = None
    media_type: str | None = None
    created_at: datetime
    is_active: bool = False


class DocumentDetail(BaseModel):
    id: UUID
    kb_id: UUID
    name: str
    created_at: datetime
    active_version_id: UUID | None = None
    versions: list[DocumentVersionInfo]


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


class ContextPart(BaseModel):
    chunk_id: UUID
    source_text: str
    anchor: SourceAnchor
    title_path: list[str]


SearchRequest.model_rebuild()
SearchHit.model_rebuild()


# V1 is intentionally frozen. V2 uses separate closed DTOs so strict consumers
# can opt in explicitly rather than receiving an unsolicited schema extension.
from pydantic import FiniteFloat

from cuekb.domain.models import Applicability, ContextRole, SourceSpan


class V2SearchFilters(SearchFilters):
    document_ids: list[UUID] = Field(default_factory=list, max_length=100)


class V2SearchRequest(SearchRequest):
    filters: V2SearchFilters = Field(default_factory=V2SearchFilters)
    max_evidence_bytes: int = Field(default=48000, ge=4096, le=128000)
    include_diagnostics: bool = False


ContextOmission = Literal[
    "budget_omitted", "candidate_limit_reached", "dependency_unresolved", "unsafe_scope_excluded"
]
RankingMethod = Literal["keyword_rrf", "rrf", "rrf_related", "rrf_then_rerank"]


class V2ContextPart(ContextPart):
    model_config = ConfigDict(extra="forbid")

    source_spans: list[SourceSpan] = Field(default_factory=list, max_length=64)
    role: ContextRole | None = None
    required: bool = False


class V2RelationEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    relation_id: UUID
    subject_id: UUID
    object_id: UUID
    relation_type: RelationType
    conditions: dict[str, Any] = Field(default_factory=dict)
    stance: Literal["supports", "refutes"]
    chunk_id: UUID


class V2SearchHit(SearchHit):
    model_config = ConfigDict(extra="forbid")

    rank: int = Field(ge=1, le=20)
    score: FiniteFloat
    score_type: Literal["rrf", "rerank"]
    stage_scores: dict[str, FiniteFloat] = Field(default_factory=dict)
    applicability: Applicability = Field(default_factory=Applicability)
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


class V2SearchResponse(SearchResponse):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["2"] = "2"
    ranking_method: RankingMethod
    ranking_version: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    diagnostics: V2SearchDiagnostics
    hits: list[V2SearchHit] = Field(max_length=20)


class CuratedSubjectInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entity_id: UUID | None = None
    display_name: str = Field(min_length=1, max_length=200)
    product_model: str | None = Field(default=None, min_length=1, max_length=200)
    software_version: str | None = Field(default=None, min_length=1, max_length=200)
    provenance_id: str | None = Field(default=None, min_length=1, max_length=500)


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


class ReprocessRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_version_id: UUID | None = None
    target_profile: Literal["evidence-v2"] = "evidence-v2"
    scope_bindings: list[ScopeBindingInput] = Field(default_factory=list, max_length=100)
    document_default: CuratedScopeInput | None = None
    auto_publish: bool = False


class RestoreRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class V2DocumentMetadata(DocumentMetadata):
    model_config = ConfigDict(extra="forbid")

    scope_bindings: list[ScopeBindingInput] = Field(default_factory=list, max_length=100)
    document_default: CuratedScopeInput | None = None


class V2DocumentVersionInfo(DocumentVersionInfo):
    processing_profile: str
    derivation_kind: Literal["ingest", "reprocess", "restore"]
    source_version_id: UUID | None = None


class V2DocumentDetail(DocumentDetail):
    versions: list[V2DocumentVersionInfo]


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
