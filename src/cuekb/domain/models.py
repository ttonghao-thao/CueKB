from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, Field


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
    status: VersionStatus = VersionStatus.UPLOADED
    created_at: datetime = Field(default_factory=utc_now)


class SourceAnchor(BaseModel):
    page: int | None = Field(default=None, ge=1)
    heading_path: list[str] = Field(default_factory=list)
    start_offset: int | None = Field(default=None, ge=0)
    end_offset: int | None = Field(default=None, ge=0)


class Chunk(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    kb_id: UUID
    document_id: UUID
    version_id: UUID
    ordinal: int = Field(ge=0)
    title_path: list[str] = Field(default_factory=list)
    source_text: str = Field(min_length=1)
    search_text: str = Field(min_length=1)
    anchor: SourceAnchor = Field(default_factory=SourceAnchor)
    metadata: dict[str, Any] = Field(default_factory=dict)


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
