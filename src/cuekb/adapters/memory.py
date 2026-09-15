from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Sequence
from threading import RLock
from uuid import UUID

from cuekb.domain.models import (
    Chunk,
    Document,
    DocumentVersion,
    Job,
    JobStatus,
    KnowledgeBase,
    VersionStatus,
    utc_now,
)

TOKEN_RE = re.compile(r"[A-Za-z0-9_.:/+-]+|[\u4e00-\u9fff]")


def tokens(text: str) -> list[str]:
    return [item.lower() for item in TOKEN_RE.findall(text)]


class InMemoryRepository:
    """Process-local development adapter for deterministic tests."""

    def __init__(self) -> None:
        self._lock = RLock()
        self.knowledge_bases: dict[UUID, KnowledgeBase] = {}
        self.documents: dict[UUID, Document] = {}
        self.versions: dict[UUID, DocumentVersion] = {}
        self.jobs: dict[UUID, Job] = {}
        self.chunks: dict[UUID, Chunk] = {}

    def create_knowledge_base(self, kb: KnowledgeBase) -> KnowledgeBase:
        with self._lock:
            self.knowledge_bases[kb.id] = kb
        return kb

    def get_knowledge_base(self, kb_id: UUID) -> KnowledgeBase | None:
        return self.knowledge_bases.get(kb_id)

    def list_knowledge_bases(self, _principal=None) -> list[dict]:
        return [
            {**kb.model_dump(), "role": "admin"}
            for kb in sorted(self.knowledge_bases.values(), key=lambda item: item.name.lower())
        ]

    def list_documents(self, kb_id: UUID, limit: int, offset: int) -> list[dict]:
        documents = sorted(
            (
                document
                for document in self.documents.values()
                if document.kb_id == kb_id and document.deleted_at is None
            ),
            key=lambda item: item.created_at,
            reverse=True,
        )[offset : offset + limit]
        result = []
        for document in documents:
            versions = sorted(
                (v for v in self.versions.values() if v.document_id == document.id),
                key=lambda item: item.created_at,
                reverse=True,
            )
            latest = versions[0] if versions else None
            active = next((v for v in versions if v.status == VersionStatus.PUBLISHED), None)
            result.append(
                {
                    **document.model_dump(exclude={"deleted_at"}),
                    "version_count": len(versions),
                    "latest_version_id": latest.id if latest else None,
                    "latest_version_status": latest.status if latest else None,
                    "active_version_id": active.id if active else None,
                    "active_business_version": active.business_version if active else None,
                }
            )
        return result

    def get_document_detail(self, document_id: UUID) -> dict | None:
        document = self.documents.get(document_id)
        if document is None or document.deleted_at is not None:
            return None
        versions = sorted(
            (v for v in self.versions.values() if v.document_id == document.id),
            key=lambda item: item.created_at,
            reverse=True,
        )
        active = next((v for v in versions if v.status == VersionStatus.PUBLISHED), None)
        return {
            **document.model_dump(exclude={"deleted_at"}),
            "active_version_id": active.id if active else None,
            "versions": [
                {
                    **version.model_dump(),
                    "original_filename": None,
                    "media_type": None,
                    "is_active": active is not None and version.id == active.id,
                }
                for version in versions
            ],
        }

    def create_document(self, document: Document, version: DocumentVersion, job: Job) -> None:
        with self._lock:
            self.documents[document.id] = document
            self.versions[version.id] = version
            self.jobs[job.id] = job

    def get_job(self, job_id: UUID) -> Job | None:
        return self.jobs.get(job_id)

    def publish_chunks(self, job_id: UUID, chunks: Sequence[Chunk]) -> Job:
        with self._lock:
            job = self.jobs[job_id]
            version = self.versions[job.version_id]
            for chunk in chunks:
                self.chunks[chunk.id] = chunk
            self.versions[version.id] = version.model_copy(
                update={"status": VersionStatus.PUBLISHED}
            )
            kb = self.knowledge_bases[job.kb_id]
            self.knowledge_bases[kb.id] = kb.model_copy(
                update={"content_revision": kb.content_revision + 1}
            )
            finished = job.model_copy(
                update={
                    "status": JobStatus.SUCCEEDED,
                    "stage": "published",
                    "progress": 100,
                    "updated_at": utc_now(),
                }
            )
            self.jobs[job.id] = finished
            return finished

    def visible_chunks(self, kb_ids: Sequence[UUID]) -> list[Chunk]:
        allowed = set(kb_ids)
        return [
            chunk
            for chunk in self.chunks.values()
            if chunk.kb_id in allowed
            and self.documents[chunk.document_id].deleted_at is None
            and self.versions[chunk.version_id].status == VersionStatus.PUBLISHED
        ]

    def load_chunks(self, ids: Sequence[UUID], kb_ids: Sequence[UUID]) -> list[Chunk]:
        allowed_ids, allowed_kbs = set(ids), set(kb_ids)
        return [
            chunk
            for chunk in self.visible_chunks(kb_ids)
            if chunk.id in allowed_ids and chunk.kb_id in allowed_kbs
        ]


class InMemorySearchBackend:
    """Deterministic test/dev retrieval; not the production OpenSearch adapter."""

    def __init__(self) -> None:
        self._chunks: dict[UUID, Chunk] = {}

    def index(
        self, chunks: Sequence[Chunk], vectors: Sequence[Sequence[float]] | None = None
    ) -> None:
        self._chunks.update({chunk.id: chunk for chunk in chunks})

    def keyword_search(
        self, query: str, kb_ids: Sequence[UUID], filters: dict, limit: int
    ) -> list[tuple[UUID, float]]:
        query_tokens = tokens(query)
        if not query_tokens:
            return []
        query_counts = Counter(query_tokens)
        scored: list[tuple[UUID, float]] = []
        allowed = set(kb_ids)
        for chunk in self._chunks.values():
            if chunk.kb_id not in allowed or not _matches(chunk, filters):
                continue
            document_tokens = tokens(chunk.search_text)
            counts = Counter(document_tokens)
            score = sum(
                (1 + math.log(counts[token])) * weight
                for token, weight in query_counts.items()
                if counts[token]
            )
            if score > 0:
                scored.append((chunk.id, score))
        return sorted(scored, key=lambda item: (-item[1], str(item[0])))[:limit]

    def vector_search(
        self, vector: Sequence[float], kb_ids: Sequence[UUID], filters: dict, limit: int
    ) -> list[tuple[UUID, float]]:
        # Vector search is deliberately unavailable in memory development mode.
        raise NotImplementedError("vector search adapter is not configured")

    def delete_document(self, document_id: UUID) -> None:
        self._chunks = {
            key: value for key, value in self._chunks.items() if value.document_id != document_id
        }


def _matches(chunk: Chunk, filters: dict) -> bool:
    document_ids = filters.get("document_ids") or []
    return (not document_ids or chunk.document_id in set(document_ids)) and all(
        not filters.get(key) or chunk.metadata.get(key) == filters[key]
        for key in ("product_model", "software_version")
    )
