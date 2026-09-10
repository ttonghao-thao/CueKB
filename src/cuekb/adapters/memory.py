from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Sequence
from threading import RLock
from uuid import UUID

from cuekb.domain.models import Chunk, Document, DocumentVersion, Job, JobStatus, KnowledgeBase, VersionStatus, utc_now


TOKEN_RE = re.compile(r"[A-Za-z0-9_.:/+-]+|[\u4e00-\u9fff]")


def tokens(text: str) -> list[str]:
    return [item.lower() for item in TOKEN_RE.findall(text)]


class InMemoryRepository:
    """Development adapter. Production adapters will preserve the same port."""

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
            self.versions[version.id] = version.model_copy(update={"status": VersionStatus.PUBLISHED})
            kb = self.knowledge_bases[job.kb_id]
            self.knowledge_bases[kb.id] = kb.model_copy(
                update={"content_revision": kb.content_revision + 1}
            )
            finished = job.model_copy(
                update={"status": JobStatus.SUCCEEDED, "stage": "published", "progress": 100, "updated_at": utc_now()}
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


class InMemorySearchBackend:
    """Deterministic test/dev retrieval; not the production OpenSearch adapter."""

    def index(self, chunks: Sequence[Chunk]) -> None:
        return None

    def keyword_search(self, query: str, chunks: Sequence[Chunk], limit: int) -> list[tuple[Chunk, float]]:
        query_tokens = tokens(query)
        if not query_tokens:
            return []
        query_counts = Counter(query_tokens)
        scored: list[tuple[Chunk, float]] = []
        for chunk in chunks:
            document_tokens = tokens(chunk.search_text)
            counts = Counter(document_tokens)
            score = sum((1 + math.log(counts[token])) * weight for token, weight in query_counts.items() if counts[token])
            if score > 0:
                scored.append((chunk, score))
        return sorted(scored, key=lambda item: (-item[1], str(item[0].id)))[:limit]

    def vector_search(self, query: str, chunks: Sequence[Chunk], limit: int) -> list[tuple[Chunk, float]]:
        # Explicitly unavailable until the BGE/OpenSearch adapter is implemented.
        raise NotImplementedError("vector search adapter is not configured")
