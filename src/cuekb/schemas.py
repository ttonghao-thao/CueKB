from datetime import datetime
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel, Field, SecretStr, model_validator

from cuekb.domain.models import (
    EvidenceStatus,
    RetrievalMode,
    RetrievalStatus,
    SourceAnchor,
    VersionStatus,
)


class KnowledgeBaseCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
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
    document_ids: list[UUID] = Field(default_factory=list)
    product_model: str | None = None
    software_version: str | None = None


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    kb_ids: list[UUID] = Field(min_length=1, max_length=50)
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
    context_parts: list["ContextPart"] = Field(default_factory=list)
    context_truncated: bool = False
    relations: list[dict[str, Any]] = Field(default_factory=list)


class SearchResponse(BaseModel):
    trace_id: UUID
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
    chunking_version: Literal["structured-v1"] = "structured-v1"


class ContextPart(BaseModel):
    chunk_id: UUID
    source_text: str
    anchor: SourceAnchor
    title_path: list[str]


SearchRequest.model_rebuild()
SearchHit.model_rebuild()
