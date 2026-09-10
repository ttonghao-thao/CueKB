from __future__ import annotations

from time import perf_counter
from uuid import UUID, uuid4

from cuekb.config import Settings
from cuekb.domain.models import EvidenceStatus, RetrievalMode, RetrievalStatus
from cuekb.ports import Repository, SearchBackend
from cuekb.schemas import SearchHit, SearchRequest, SearchResponse


def rrf(rankings: list[list[UUID]], rank_constant: int = 60) -> dict[UUID, float]:
    scores: dict[UUID, float] = {}
    for ranking in rankings:
        for rank, item_id in enumerate(ranking, start=1):
            scores[item_id] = scores.get(item_id, 0.0) + 1.0 / (rank_constant + rank)
    return scores


class RetrievalService:
    def __init__(self, repository: Repository, search: SearchBackend, settings: Settings) -> None:
        self.repository = repository
        self.search = search
        self.settings = settings

    def search_evidence(self, request: SearchRequest) -> SearchResponse:
        started = perf_counter()
        missing = [kb_id for kb_id in request.kb_ids if self.repository.get_knowledge_base(kb_id) is None]
        if missing:
            raise KeyError("knowledge_base_not_found")
        chunks = self.repository.visible_chunks(request.kb_ids)
        if request.filters.document_ids:
            allowed_documents = set(request.filters.document_ids)
            chunks = [chunk for chunk in chunks if chunk.document_id in allowed_documents]
        if request.filters.product_model:
            chunks = [chunk for chunk in chunks if chunk.metadata.get("product_model") == request.filters.product_model]
        if request.filters.software_version:
            chunks = [chunk for chunk in chunks if chunk.metadata.get("software_version") == request.filters.software_version]

        lookup = {chunk.id: chunk for chunk in chunks}
        degraded: list[str] = []
        keyword_started = perf_counter()
        keyword = self.search.keyword_search(request.query, chunks, self.settings.keyword_candidates)
        keyword_ms = (perf_counter() - keyword_started) * 1000

        vector: list[tuple[object, float]] = []
        vector_started = perf_counter()
        if request.mode not in {RetrievalMode.EXACT}:
            try:
                vector = self.search.vector_search(request.query, chunks, self.settings.vector_candidates)
            except NotImplementedError:
                degraded.append("vector_unavailable")
        vector_ms = (perf_counter() - vector_started) * 1000

        rankings = [[item.id for item, _ in keyword]]
        if vector:
            rankings.append([item.id for item, _ in vector])
        scores = rrf(rankings)
        ordered = sorted(scores, key=lambda item_id: (-scores[item_id], str(item_id)))[: request.top_k]
        hits = [
            SearchHit(
                chunk_id=chunk_id,
                document_id=lookup[chunk_id].document_id,
                version_id=lookup[chunk_id].version_id,
                rank=rank,
                source_text=lookup[chunk_id].source_text,
                context=lookup[chunk_id].source_text if request.include_context else None,
                title_path=lookup[chunk_id].title_path,
                anchor=lookup[chunk_id].anchor,
                metadata=lookup[chunk_id].metadata,
                retrieval_sources=[
                    source
                    for source, candidates in (("keyword", keyword), ("vector", vector))
                    if any(candidate.id == chunk_id for candidate, _ in candidates)
                ],
            )
            for rank, chunk_id in enumerate(ordered, start=1)
        ]
        if not hits:
            status = RetrievalStatus.NOT_FOUND
        elif degraded:
            status = RetrievalStatus.DEGRADED
        else:
            status = RetrievalStatus.OK
        revisions = {
            str(kb_id): self.repository.get_knowledge_base(kb_id).content_revision  # type: ignore[union-attr]
            for kb_id in request.kb_ids
        }
        total_ms = (perf_counter() - started) * 1000
        return SearchResponse(
            trace_id=uuid4(),
            retrieval_status=status,
            evidence_status=EvidenceStatus.UNASSESSED,
            degraded_reasons=degraded,
            scope_limited=request.mode == RetrievalMode.RELATED,
            content_revisions=revisions,
            timings_ms={"keyword": keyword_ms, "vector": vector_ms, "total": total_ms},
            hits=hits,
        )
