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
        result = []
        for chunk in self.visible_chunks(kb_ids):
            if chunk.id not in allowed_ids or chunk.kb_id not in allowed_kbs:
                continue
            document = self.documents[chunk.document_id]
            version = self.versions[chunk.version_id]
            result.append(
                chunk.model_copy(
                    update={
                        "document_name": document.name,
                        "content_sha256": version.content_sha256,
                    }
                )
            )
        return result

    def context_chunks_v2(self, chunk, limit, include_optional=True):
        from cuekb.services.evidence import scope_compatible

        links = {link.chunk_id: link for link in chunk.evidence_structure.context_links}
        candidates = [c for c in self.visible_chunks([chunk.kb_id])
            if c.document_id == chunk.document_id and c.version_id == chunk.version_id
            and (c.id == chunk.id or c.id in links or (include_optional and (
                c.evidence_structure.source_unit_id == chunk.evidence_structure.source_unit_id
                or (c.title_path == chunk.title_path and abs(c.ordinal - chunk.ordinal) <= 1))))
            and scope_compatible(chunk, c,
                required=links[c.id].required if c.id in links else False,
                role=links[c.id].role if c.id in links else None)]
        return sorted(candidates, key=lambda c: (c.id != chunk.id, c.id not in links,
            abs(c.ordinal - chunk.ordinal), c.ordinal))[:max(limit, len(links) + 1)]

    def related_chunks(self, request, seed_ids, limit, timeout_ms):
        return []


class InMemorySearchBackend:
    """Deterministic test/dev retrieval; not the production OpenSearch adapter."""

    def __init__(self) -> None:
        self._chunks: dict[UUID, Chunk] = {}

    def index(
        self, chunks: Sequence[Chunk], vectors: Sequence[Sequence[float]] | None = None
    ) -> None:
        self._chunks.update({chunk.id: chunk for chunk in chunks})

    def keyword_search(
        self,
        query: str,
        kb_ids: Sequence[UUID],
        filters: dict,
        limit: int,
        phrase: bool = False,
    ) -> list[tuple[UUID, float]]:
        query_tokens = tokens(query)
        if not query_tokens:
            return []
        query_counts = Counter(query_tokens)
        scored: list[tuple[UUID, float]] = []
        allowed = set(kb_ids)
        lowered = query.lower()
        for chunk in self._chunks.values():
            if chunk.kb_id not in allowed or not _matches(chunk, filters):
                continue
            if chunk.evidence_structure.block_kind == "heading":
                continue
            if phrase:
                identifier_hit = any(
                    query in {s.product_model, s.software_version}
                    for s in chunk.evidence_structure.applicability.subjects
                ) and chunk.evidence_structure.applicability.status == "resolved"
                if lowered in chunk.search_text.lower() or identifier_hit:
                    scored.append((chunk.id, 1.0))
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

    async def keyword_search_async(self, query, kb_ids, filters, limit, phrase=False):
        return self.keyword_search(query, kb_ids, filters, limit, phrase)

    async def vector_search_async(self, vector, kb_ids, filters, limit):
        return self.vector_search(vector, kb_ids, filters, limit)

    def delete_document(self, document_id: UUID) -> None:
        self._chunks = {
            key: value for key, value in self._chunks.items() if value.document_id != document_id
        }


def _matches(chunk: Chunk, filters: dict) -> bool:
    from cuekb.services.evidence import scope_matches
    from cuekb.services.selection import chunk_identifiers

    document_ids = filters.get("document_ids") or []
    query_ids = set(filters.get("_query_identifiers") or [])
    return (
        (not document_ids or chunk.document_id in set(document_ids))
        and (not filters.get("category") or chunk.metadata.get("category") == filters["category"])
        and scope_matches(chunk, filters.get("product_model"), filters.get("software_version"))
        and (not query_ids or bool(query_ids & chunk_identifiers(chunk)))
    )
