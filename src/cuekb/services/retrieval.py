from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from copy import copy
from time import perf_counter
from uuid import UUID, uuid4

from cuekb.config import Settings
from cuekb.domain.models import EvidenceStatus, RetrievalMode, RetrievalStatus
from cuekb.ports import ModelClient, Repository, SearchBackend
from cuekb.schemas import SearchHit, SearchRequest, SearchResponse
from cuekb.services.context import assemble_context


def rrf(rankings: list[list[UUID]], rank_constant: int = 60) -> dict[UUID, float]:
    scores: dict[UUID, float] = {}
    for ranking in rankings:
        for rank, item_id in enumerate(ranking, start=1):
            scores[item_id] = scores.get(item_id, 0.0) + 1.0 / (rank_constant + rank)
    return scores


class QueryRouter:
    def __init__(self, identifier_pattern: str) -> None:
        self.identifier = re.compile(identifier_pattern)

    def route(self, request: SearchRequest) -> tuple[str, str]:
        if request.mode == RetrievalMode.EXACT:
            return "exact", "caller_requested_exact"
        if request.mode == RetrievalMode.RELATED:
            return "related", "caller_requested_related"
        if request.mode == RetrievalMode.HYBRID:
            return "hybrid", "caller_requested_hybrid"
        if self.identifier.fullmatch(request.query.strip()):
            return "exact", "auto_exact_identifier"
        return "hybrid", "auto_natural_language"


class RetrievalService:
    def __init__(
        self,
        repository: Repository,
        search: SearchBackend,
        settings: Settings,
        model: ModelClient | None = None,
        reranker_configured: bool = False,
    ) -> None:
        self.repository, self.search, self.settings, self.model = (
            repository,
            search,
            settings,
            model,
        )
        self.router = QueryRouter(settings.exact_identifier_pattern)
        self.reranker_configured = reranker_configured

    def search_evidence(
        self, request: SearchRequest, _retry_on_transition: bool = True, principal=None
    ) -> SearchResponse:
        started = perf_counter()
        snapshot = getattr(self.repository, "read_snapshot", None)
        if snapshot is None:
            return self._search_evidence(request, _retry_on_transition)
        # One repeatable-read snapshot covers all authoritative reads in each attempt.
        for attempt in range(2):
            with snapshot() as scoped:
                scoped.require_role(principal, request.kb_ids, "read")
                service = copy(self)
                service.repository = scoped
                result = service._search_evidence(request, False)
            # A fresh transaction is mandatory: repeatable-read alone would hide revocations.
            with snapshot() as latest:
                if principal and principal.api_key_id:
                    latest.assert_api_key_active(principal.api_key_id)
                latest.require_role(principal, request.kb_ids, "read")
                revisions = {
                    str(k): latest.get_knowledge_base(k).content_revision for k in request.kb_ids
                }
                if revisions != result.content_revisions and attempt == 0:
                    continue
                current = {
                    c.id: c
                    for c in latest.load_chunks([h.chunk_id for h in result.hits], request.kb_ids)
                }
                relations = []
                if request.mode == RetrievalMode.RELATED and "relation" in result.executed_stages:
                    try:
                        seeds = [
                            c.id
                            for c in latest.load_chunks(
                                getattr(service, "_relation_seed_ids", []), request.kb_ids
                            )
                        ]
                        relations = latest.related_chunks(
                            request,
                            seeds,
                            self.settings.relation_candidates,
                            self.settings.relation_timeout_ms,
                        )
                    except Exception as exc:
                        from sqlalchemy.exc import DBAPIError

                        if (
                            not isinstance(exc, DBAPIError)
                            or getattr(exc.orig, "sqlstate", None) != "57014"
                        ):
                            raise
                        result.degraded_reasons.append("relation_unavailable")
                relation_ids = {r["chunk_id"] for r in relations}
                result.hits = [
                    h
                    for h in result.hits
                    if h.chunk_id in current
                    and (h.retrieval_sources != ["relation"] or h.chunk_id in relation_ids)
                ]
                budget = self.settings.context_max_chars
                for rank, hit in enumerate(result.hits, 1):
                    hit.rank = rank
                    hit.relations = [r for r in relations if r["chunk_id"] == hit.chunk_id]
                    if not hit.relations:
                        hit.retrieval_sources = [
                            s for s in hit.retrieval_sources if s != "relation"
                        ]
                    hit.context = None
                    hit.context_parts = []
                    if request.include_context:
                        chunk = current[hit.chunk_id]
                        neighbors = latest.context_chunks(chunk, self.settings.context_max_chunks)
                        hit.context, hit.context_parts, hit.context_truncated = assemble_context(
                            chunk, neighbors, min(budget, self.settings.context_per_hit_chars)
                        )
                        hit.context_truncated = (
                            hit.context_truncated
                            or len(neighbors) >= self.settings.context_max_chunks
                        )
                        budget -= len(hit.context)
                if revisions != result.content_revisions:
                    result.degraded_reasons.append("index_transition")
                result.content_revisions = revisions
                result.retrieval_status = (
                    RetrievalStatus.NOT_FOUND
                    if not result.hits
                    else RetrievalStatus.DEGRADED
                    if result.degraded_reasons
                    else RetrievalStatus.OK
                )
                result.timings_ms["total"] = (perf_counter() - started) * 1000
                return result
        raise RuntimeError("unreachable")

    def _search_evidence(
        self, request: SearchRequest, _retry_on_transition: bool = True
    ) -> SearchResponse:
        started = perf_counter()
        deadline = started + self.settings.search_deadline_ms / 1000
        initial_revisions: dict[str, int] = {}
        for kb in request.kb_ids:
            knowledge_base = self.repository.get_knowledge_base(kb)
            if knowledge_base is None:
                raise KeyError("knowledge_base_not_found")
            initial_revisions[str(kb)] = knowledge_base.content_revision
        path, route_reason = self.router.route(request)
        filters = request.filters.model_dump()
        degraded: list[str] = []
        executed: list[str] = []
        skipped: list[dict[str, str]] = [{"stage": "route", "reason": route_reason}]
        timings: dict[str, float] = {}

        def keyword_call():
            mark = perf_counter()
            value = self.search.keyword_search(
                request.query, request.kb_ids, filters, self.settings.keyword_candidates
            )
            timings["keyword"] = (perf_counter() - mark) * 1000
            return value

        def vector_call():
            if self.model is None:
                raise RuntimeError("model_client_unconfigured")
            timeout = min(
                self.settings.model_timeout_ms, max(20, int((deadline - perf_counter()) * 1000))
            )
            mark = perf_counter()
            vector = self.model.embed([request.query], timeout)[0]
            timings["embedding"] = (perf_counter() - mark) * 1000
            mark = perf_counter()
            value = self.search.vector_search(
                vector, request.kb_ids, filters, self.settings.vector_candidates
            )
            timings["vector"] = (perf_counter() - mark) * 1000
            return value

        with ThreadPoolExecutor(max_workers=2) as pool:
            keyword_future = pool.submit(keyword_call)
            vector_future = pool.submit(vector_call) if path != "exact" else None
            keyword = keyword_future.result()
            executed.append("keyword")
            vector = []
            if vector_future:
                try:
                    vector = vector_future.result()
                    executed.extend(["embedding", "vector"])
                except Exception:
                    degraded.append("vector_unavailable")
                    skipped.append({"stage": "vector", "reason": "model_or_search_unavailable"})
            else:
                skipped.append({"stage": "embedding", "reason": "exact_path"})

        rankings = [[item_id for item_id, _ in keyword]]
        if vector:
            rankings.append([item_id for item_id, _ in vector])
        relation_rows = []
        if path == "related":
            remaining = int((deadline - perf_counter()) * 1000)
            if remaining > 0:
                try:
                    seeds = [
                        c.id
                        for c in self.repository.load_chunks(
                            [i for ranking in rankings for i in ranking], request.kb_ids
                        )
                        if self._matches(c, filters)
                    ]
                    self._relation_seed_ids = seeds
                    relation_rows = self.repository.related_chunks(
                        request,
                        seeds,
                        self.settings.relation_candidates,
                        min(remaining, self.settings.relation_timeout_ms),
                    )
                    rankings.append(list(dict.fromkeys(r["chunk_id"] for r in relation_rows)))
                    executed.append("relation")
                except Exception as exc:
                    from sqlalchemy.exc import DBAPIError

                    if (
                        not isinstance(exc, DBAPIError)
                        or getattr(exc.orig, "sqlstate", None) != "57014"
                    ):
                        raise
                    degraded.append("relation_unavailable")
            else:
                degraded.append("relation_budget_exhausted")
        scores = rrf(rankings)
        executed.append("rrf")
        candidate_ids = sorted(scores, key=lambda item_id: (-scores[item_id], str(item_id)))
        chunks = self.repository.load_chunks(candidate_ids, request.kb_ids)
        by_id = {chunk.id: chunk for chunk in chunks}
        ordered = [
            item_id
            for item_id in candidate_ids
            if item_id in by_id and self._matches(by_id[item_id], filters)
        ]
        rerank_head = ordered[: self.settings.rerank_candidates]
        tail = ordered[self.settings.rerank_candidates :]

        remaining_ms = (deadline - perf_counter()) * 1000
        can_rerank = (
            self.settings.rerank_enabled
            and self.reranker_configured
            and path != "exact"
            and self.model is not None
            and len(rerank_head) > 1
        )
        if can_rerank and remaining_ms >= self.settings.rerank_min_remaining_ms:
            try:
                mark = perf_counter()
                model = self.model
                if model is None:
                    raise RuntimeError("model_client_unconfigured")
                values = model.rerank(
                    request.query,
                    [by_id[item].source_text for item in rerank_head],
                    min(self.settings.model_timeout_ms, int(remaining_ms)),
                )
                rerank_head = [
                    item
                    for _, item in sorted(
                        zip(values, rerank_head, strict=True),
                        key=lambda pair: (-pair[0], str(pair[1])),
                    )
                ]
                timings["rerank"] = (perf_counter() - mark) * 1000
                executed.append("rerank")
            except Exception:
                degraded.append("rerank_unavailable")
                skipped.append({"stage": "rerank", "reason": "model_unavailable_or_timeout"})
        else:
            if path == "exact":
                reason = "exact_path"
            elif not self.reranker_configured:
                reason = "reranker_service_unconfigured"
            elif not self.settings.rerank_enabled:
                reason = "rerank_disabled"
            elif self.model is None:
                reason = "model_client_unconfigured"
            elif len(rerank_head) <= 1:
                reason = "not_enough_candidates"
            else:
                reason = "insufficient_remaining_budget"
            skipped.append({"stage": "rerank", "reason": reason})

        ordered = rerank_head + tail

        # Reranking can take hundreds of milliseconds. Reload candidates after
        # it so a concurrent publish or delete cannot return a now-stale version.
        current_chunks = self.repository.load_chunks(ordered, request.kb_ids)
        by_id = {chunk.id: chunk for chunk in current_chunks}
        ordered = [item_id for item_id in ordered if item_id in by_id]

        keyword_ids, vector_ids = {item for item, _ in keyword}, {item for item, _ in vector}
        hits = []
        context_budget = self.settings.context_max_chars
        for rank, chunk_id in enumerate(ordered[: request.top_k], 1):
            chunk = by_id[chunk_id]
            context, parts, truncated = None, [], False
            if request.include_context:
                context, parts, truncated = assemble_context(
                    chunk,
                    self.repository.context_chunks(chunk, self.settings.context_max_chunks),
                    min(context_budget, self.settings.context_per_hit_chars),
                )
                truncated = truncated or len(parts) >= self.settings.context_max_chunks
                context_budget -= len(context)
            hits.append(
                SearchHit(
                    chunk_id=chunk.id,
                    document_id=chunk.document_id,
                    version_id=chunk.version_id,
                    rank=rank,
                    source_text=chunk.source_text,
                    context=context,
                    context_parts=parts,
                    context_truncated=truncated,
                    relations=[r for r in relation_rows if r["chunk_id"] == chunk_id],
                    title_path=chunk.title_path,
                    anchor=chunk.anchor,
                    metadata=chunk.metadata,
                    retrieval_sources=[
                        name
                        for name, ids in (
                            ("keyword", keyword_ids),
                            ("vector", vector_ids),
                            ("relation", {r["chunk_id"] for r in relation_rows}),
                        )
                        if chunk_id in ids
                    ],
                )
            )
        status = (
            RetrievalStatus.NOT_FOUND
            if not hits
            else RetrievalStatus.DEGRADED
            if degraded
            else RetrievalStatus.OK
        )
        revisions = {}
        for kb in request.kb_ids:
            knowledge_base = self.repository.get_knowledge_base(kb)
            if knowledge_base is None:
                raise KeyError("knowledge_base_not_found")
            revisions[str(kb)] = knowledge_base.content_revision
        if revisions != initial_revisions:
            if _retry_on_transition:
                return self._search_evidence(request, _retry_on_transition=False)
            degraded.append("index_transition")
            valid_ids = {
                chunk.id
                for chunk in self.repository.load_chunks(
                    [hit.chunk_id for hit in hits], request.kb_ids
                )
            }
            hits = [
                hit.model_copy(update={"rank": rank})
                for rank, hit in enumerate(
                    (hit for hit in hits if hit.chunk_id in valid_ids), start=1
                )
            ]
            status = RetrievalStatus.DEGRADED if hits else RetrievalStatus.NOT_FOUND
        timings["total"] = (perf_counter() - started) * 1000
        return SearchResponse(
            trace_id=uuid4(),
            retrieval_status=status,
            evidence_status=EvidenceStatus.UNASSESSED,
            degraded_reasons=degraded,
            scope_limited=path == "related",
            content_revisions=revisions,
            timings_ms=timings,
            retrieval_path=path,
            executed_stages=executed,
            skipped_stages=skipped,
            hits=hits,
        )

    @staticmethod
    def _matches(chunk, filters: dict) -> bool:
        documents = filters.get("document_ids") or []
        return (not documents or chunk.document_id in set(documents)) and all(
            not filters.get(key) or chunk.metadata.get(key) == filters[key]
            for key in ("product_model", "software_version")
        )
