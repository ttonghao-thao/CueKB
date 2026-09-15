from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

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
    kb_ids: list[UUID] = Field(min_length=1)
    mode: RetrievalMode = RetrievalMode.AUTO
    top_k: int = Field(default=8, ge=1, le=20)
    filters: SearchFilters = Field(default_factory=SearchFilters)
    include_context: bool = True

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
