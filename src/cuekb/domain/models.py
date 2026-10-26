from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
import hashlib
import json
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


PROCESSING_REVISION = "evidence-v2.4"


def default_processing_snapshot() -> dict[str, Any]:
    """Current empty-document processing inputs, freshly allocated per version."""
    return {
        "profile_revision": "evidence-v2",
        "parser_revision": PROCESSING_REVISION,
        "chunker_revision": PROCESSING_REVISION,
        "structure_revision": PROCESSING_REVISION,
        "max_chunk_chars": 1200,
        "scope_bindings": [],
        "dependency_bindings": [],
        "approved_entities": [],
    }


DEFAULT_PROCESSING_PROFILE = "evidence-v2:" + hashlib.sha256(json.dumps(
    default_processing_snapshot(), sort_keys=True, ensure_ascii=False, separators=(",", ":")
).encode()).hexdigest()


def utc_now() -> datetime:
    return datetime.now(UTC)


class VersionStatus(StrEnum):
    UPLOADED = "uploaded"
    PARSING = "parsing"
    NEEDS_REVIEW = "needs_review"
    INDEXING = "indexing"
    READY = "ready"
    PUBLISHED = "published"
    SUPERSEDED = "superseded"
    FAILED = "failed"


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class RetrievalMode(StrEnum):
    AUTO = "auto"
    EXACT = "exact"
    HYBRID = "hybrid"
    RELATED = "related"


class RetrievalStatus(StrEnum):
    OK = "ok"
    DEGRADED = "degraded"
    NOT_FOUND = "not_found"
    NEEDS_CLARIFICATION = "needs_clarification"


class EvidenceStatus(StrEnum):
    UNASSESSED = "unassessed"
    SUFFICIENT = "sufficient"
    INSUFFICIENT = "insufficient"
    CONFLICTING = "conflicting"


class KnowledgeBase(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    name: str = Field(min_length=1, max_length=200)
    description: str = ""
    content_revision: int = 0
    acl_revision: int = 0
    created_at: datetime = Field(default_factory=utc_now)


class Document(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    kb_id: UUID
    name: str = Field(min_length=1, max_length=500)
    created_at: datetime = Field(default_factory=utc_now)
    deleted_at: datetime | None = None


class DocumentVersion(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    document_id: UUID
    content_sha256: str = Field(min_length=64, max_length=64)
    business_version: str | None = None
    scope: dict[str, Any] = Field(default_factory=dict)
    processing_profile: str = DEFAULT_PROCESSING_PROFILE
    processing_snapshot: dict[str, Any] = Field(default_factory=default_processing_snapshot)
    derivation_kind: Literal["ingest", "reprocess", "restore"] = "ingest"
    source_version_id: UUID | None = None
    status: VersionStatus = VersionStatus.UPLOADED
    created_at: datetime = Field(default_factory=utc_now)


class SourceAnchor(BaseModel):
    page: int | None = Field(default=None, ge=1)
    heading_path: list[str] = Field(default_factory=list)
    start_offset: int | None = Field(default=None, ge=0)
    end_offset: int | None = Field(default=None, ge=0)
    bbox: dict[str, float | str] | None = None


class StructureModel(BaseModel):
    """Trusted structure is a closed contract, separate from upload metadata."""

    model_config = ConfigDict(extra="forbid")


class SourceSpan(StructureModel):
    label: str = Field(default="text", min_length=1, max_length=100)
    text: str = Field(min_length=1, max_length=32768)
    anchor: SourceAnchor = Field(default_factory=SourceAnchor)
    parser_item_id: str | None = Field(default=None, max_length=500)
    span_id: str | None = Field(default=None, max_length=100)
    row_index: int | None = Field(default=None, ge=0)
    column_index: int | None = Field(default=None, ge=0)


class ApplicableSubject(StructureModel):
    entity_id: UUID | None = None
    display_name: str = Field(min_length=1, max_length=200)
    product_model: str | None = Field(default=None, min_length=1, max_length=200)
    software_version: str | None = Field(default=None, min_length=1, max_length=200)
    source_kind: Literal["curated", "reviewed_alias", "parser"] = "curated"
    provenance_id: str | None = Field(default=None, max_length=500)
    source_spans: list[SourceSpan] = Field(default_factory=list, max_length=20)


class ApplicableCondition(StructureModel):
    text: str = Field(min_length=1, max_length=2000)
    source_spans: list[SourceSpan] = Field(default_factory=list, max_length=20)
    source_kind: Literal["curated", "parser"] = "curated"
    provenance_id: str | None = Field(default=None, max_length=500)


ScopeReason = Literal["missing_binding", "no_reviewed_match", "structure_missing", "scope_conflict"]
DependencyIssue = Literal[
    "unmatched_reference", "ambiguous_reference", "dependency_limit_exceeded", "dependency_cycle", "unsafe_dependency"
]


class Applicability(StructureModel):
    status: Literal["resolved", "unknown", "ambiguous"] = "unknown"
    subjects: list[ApplicableSubject] = Field(default_factory=list, max_length=8)
    conditions: list[ApplicableCondition] = Field(default_factory=list, max_length=20)
    reason_codes: list[ScopeReason] = Field(default_factory=list, max_length=4)

    @model_validator(mode="after")
    def resolved_has_subject(self) -> "Applicability":
        if self.status == "resolved" and not self.subjects:
            raise ValueError("resolved_applicability_requires_subject")
        return self


ContextRole = Literal["attribution", "table_header", "condition", "exception", "continuation"]


class ContextProvenance(StructureModel):
    kind: Literal["parser_reference", "curated", "parser_continuation"]
    source_unit_id: str = Field(min_length=1, max_length=200)
    target_unit_id: str = Field(min_length=1, max_length=200)
    provenance_id: str = Field(min_length=1, max_length=500)
    reference_text: str | None = Field(default=None, min_length=1, max_length=500)


class ContextLink(StructureModel):
    chunk_id: UUID
    role: ContextRole
    required: bool = True
    provenance: ContextProvenance | None = None


class EvidenceStructure(StructureModel):
    block_kind: Literal["text", "list", "table", "heading"] = "text"
    source_unit_id: str | None = Field(default=None, max_length=200)
    origin_source_unit_id: str | None = Field(default=None, max_length=200)
    parser_unit_key: str | None = Field(default=None, max_length=1024)
    parent_unit_id: str | None = Field(default=None, max_length=200)
    review_parent_id: UUID | None = None
    fact_kind: Literal["statement", "specification", "comparison", "bundle", "general"] = "statement"
    review_state: Literal["pending", "rule_confirmed", "human_confirmed", "deferred", "excluded"] = "pending"
    review_reason: str | None = Field(default=None, max_length=200)
    review_provenance: str | None = Field(default=None, max_length=500)
    applicability: Applicability = Field(default_factory=Applicability)
    context_links: list[ContextLink] = Field(default_factory=list, max_length=20)
    structure_revision: str = Field(default=PROCESSING_REVISION, min_length=1, max_length=100)
    dependency_unresolved: bool = False
    dependency_issues: list[DependencyIssue] = Field(default_factory=list, max_length=5)
    # Frozen reviewed aliases for lexical projection; not a user-facing scope assertion.
    retrieval_aliases: list[str] = Field(default_factory=list, max_length=800)


class Chunk(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    kb_id: UUID
    document_id: UUID
    version_id: UUID
    section_id: UUID | None = None
    ordinal: int = Field(ge=0)
    title_path: list[str] = Field(default_factory=list)
    source_text: str = Field(min_length=1)
    search_text: str = Field(min_length=1)
    anchor: SourceAnchor = Field(default_factory=SourceAnchor)
    metadata: dict[str, Any] = Field(default_factory=dict)
    source_spans: list[SourceSpan] = Field(default_factory=list, max_length=64)
    evidence_structure: EvidenceStructure = Field(default_factory=EvidenceStructure)
    processing_profile: str = DEFAULT_PROCESSING_PROFILE
    # Transport-only fields joined from documents/document_versions on authoritative reads.
    document_name: str | None = None
    content_sha256: str | None = None


class Job(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    kb_id: UUID
    document_id: UUID
    version_id: UUID
    status: JobStatus = JobStatus.QUEUED
    stage: str = "uploaded"
    progress: int = Field(default=0, ge=0, le=100)
    error_code: str | None = None
    error_message: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class Principal(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    name: str
    is_system_admin: bool = False
    api_key_id: UUID | None = None


class RetrievalPlan(BaseModel):
    path: str
    use_embedding: bool
    use_rerank: bool
    reasons: list[str] = Field(default_factory=list)


class Section(BaseModel):
    id: UUID
    version_id: UUID
    parent_id: UUID | None = None
    ordinal: int
    title_path: list[str]
    content: str
