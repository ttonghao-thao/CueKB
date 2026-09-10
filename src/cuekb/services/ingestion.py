from __future__ import annotations

import hashlib
import re
from uuid import UUID

from cuekb.domain.models import Chunk, Document, DocumentVersion, Job, KnowledgeBase, SourceAnchor
from cuekb.ports import Repository, SearchBackend
from cuekb.schemas import DocumentCreate, KnowledgeBaseCreate


class IngestionService:
    def __init__(self, repository: Repository, search: SearchBackend) -> None:
        self.repository = repository
        self.search = search

    def create_knowledge_base(self, request: KnowledgeBaseCreate) -> KnowledgeBase:
        return self.repository.create_knowledge_base(KnowledgeBase(**request.model_dump()))

    def ingest_text(self, request: DocumentCreate) -> Job:
        if self.repository.get_knowledge_base(request.kb_id) is None:
            raise KeyError("knowledge_base_not_found")
        raw = request.content.encode("utf-8")
        document = Document(kb_id=request.kb_id, name=request.name)
        version = DocumentVersion(
            document_id=document.id,
            content_sha256=hashlib.sha256(raw).hexdigest(),
            business_version=request.business_version,
            scope=request.scope,
        )
        job = Job(kb_id=request.kb_id, document_id=document.id, version_id=version.id)
        self.repository.create_document(document, version, job)
        chunks = self._chunk_text(request, document.id, version.id)
        self.search.index(chunks)
        return self.repository.publish_chunks(job.id, chunks)

    @staticmethod
    def _chunk_text(request: DocumentCreate, document_id: UUID, version_id: UUID) -> list[Chunk]:
        paragraphs = [part.strip() for part in re.split(r"\n\s*\n", request.content) if part.strip()]
        chunks: list[Chunk] = []
        offset = 0
        for ordinal, paragraph in enumerate(paragraphs):
            start = request.content.find(paragraph, offset)
            end = start + len(paragraph)
            offset = end
            chunks.append(
                Chunk(
                    kb_id=request.kb_id,
                    document_id=document_id,
                    version_id=version_id,
                    ordinal=ordinal,
                    source_text=paragraph,
                    search_text=f"{request.name}\n{paragraph}",
                    anchor=SourceAnchor(start_offset=start, end_offset=end),
                    metadata={"business_version": request.business_version, **request.scope},
                )
            )
        return chunks
