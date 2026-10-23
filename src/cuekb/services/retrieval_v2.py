"""Bounded, attributable evidence retrieval; the V1 pipeline stays frozen.

OpenSearch supplies candidate identities, PostgreSQL supplies every returned
fact and dependency. A fresh final snapshot revalidates authorization, versions,
relations and context after optional model I/O.
"""
from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import math
from contextlib import nullcontext
from dataclasses import dataclass, field
from time import perf_counter
from typing import Any, Callable, TypeVar
from uuid import UUID

from cuekb.config import Settings
from cuekb.domain.models import Chunk, EvidenceStatus, RetrievalStatus
from cuekb.schemas import (
    V2BackendCalls, V2CandidateCounts, V2ContextPart, V2OmissionCounts,
    V2PayloadSizes, V2RelationEvidence, V2RerankerDiagnostics, V2SearchDiagnostics, V2SearchHit,
    V2SearchRequest, V2SearchResponse,
)
from cuekb.services.evidence import build_search_text, scope_matches
from cuekb.services.execution import (
    SearchConfigurationChanged, SearchDeadlineExceeded, SearchExecution,
    activate_execution, current_execution,
)
from cuekb.services.retrieval import QueryRouter, rrf

T = TypeVar("T")
ALGORITHM_REVISION = "evidence-ranking-v2.1"


def _encode(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, separators=(",", ":")
    ).encode("utf-8")


def encode_v2_hits(hits: list[V2SearchHit]) -> bytes:
    return _encode([_hit_payload(hit) for hit in hits])


def _hit_payload(hit: V2SearchHit) -> dict:
    value = hit.model_dump(mode="json")
    if not hit.stage_scores:
        value.pop("stage_scores", None)
    return value


def encode_v2_response(response: V2SearchResponse) -> bytes:
    """The route must send this exact buffer, without a second JSON encoder."""
    value = response.model_dump(mode="json")
    value["hits"] = [_hit_payload(hit) for hit in response.hits]
    return _encode(value)


@dataclass
class _Attempt:
    path: str
    route_reason: str
    keyword: list[tuple[UUID, float]] = field(default_factory=list)
    vector: list[tuple[UUID, float]] = field(default_factory=list)
    relations: list[dict] = field(default_factory=list)
    seeds: list[UUID] = field(default_factory=list)
    revisions: dict[str, int] = field(default_factory=dict)
    ordered: list[UUID] = field(default_factory=list)
    chunks: dict[UUID, Chunk] = field(default_factory=dict)
    scores: dict[UUID, float] = field(default_factory=dict)
    rerank_scores: dict[UUID, float] = field(default_factory=dict)
    degraded: list[str] = field(default_factory=list)
    executed: list[str] = field(default_factory=list)
    skipped: list[dict[str, str]] = field(default_factory=list)
    counts: V2CandidateCounts = field(default_factory=V2CandidateCounts)
    omissions: V2OmissionCounts = field(default_factory=V2OmissionCounts)
    reranker_executed: bool = False
    reranker_skip: str | None = None
    keyword_ok: bool = False
    vector_ok: bool = False
    keyword_limit: int = 50
    vector_limit: int = 50
    relation_limit: int = 30
    rerank_window: int = 30


class V2RetrievalService:
    def __init__(
        self, repository, search, settings: Settings, model=None,
        reranker_configured: bool = False, ranking_config: dict | None = None,
    ) -> None:
        self.repository = repository
        self.search = search
        self.settings = settings
        self.model = model
        self.reranker_configured = reranker_configured
        self.ranking_config = dict(ranking_config or {})
        self.router = QueryRouter(settings.exact_identifier_pattern)

    def search_evidence(
        self, request: V2SearchRequest, principal=None, execution: SearchExecution | None = None,
    ) -> V2SearchResponse:
        """Synchronous entry point for fixtures and non-ASGI embedding only."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.search_evidence_async(request, principal, execution))
        raise RuntimeError("use_search_evidence_async_inside_event_loop")

    async def search_evidence_async(
        self, request: V2SearchRequest, principal=None, execution: SearchExecution | None = None,
    ) -> V2SearchResponse:
        execution = execution or current_execution() or SearchExecution.create(
            self.settings.search_deadline_ms
        )
        with activate_execution(execution):
            try:
                # Native async HTTP is cancelled and awaited by asyncio. PostgreSQL
                # read_operation owns cancellation and cleanup of its worker thread.
                async with asyncio.timeout(execution.remaining_ms() / 1000):
                    return await self._run(request, principal, execution)
            except SearchDeadlineExceeded:
                raise
            except TimeoutError as exc:
                raise SearchDeadlineExceeded("search_deadline_exceeded") from exc

    async def _invoke(self, target, method: str, *args, **kwargs):
        asynchronous = getattr(target, f"{method}_async", None)
        if asynchronous is not None:
            return await asynchronous(*args, **kwargs)
        # Development adapters are synchronous. Production adapters expose native
        # async methods; the fallback is not a claim of cancellable third-party I/O.
        return await asyncio.to_thread(getattr(target, method), *args, **kwargs)

    async def _read(self, operation: Callable[[], T]) -> T:
        runner = getattr(self.repository, "read_operation", None)
        if runner is not None:
            result = runner(operation)
            return await result if inspect.isawaitable(result) else result
        return await asyncio.to_thread(operation)

    def _snapshot(self):
        snapshot = getattr(self.repository, "read_snapshot", None)
        return snapshot() if snapshot else nullcontext(self.repository)

    def _authorize(self, repository, request, principal) -> None:
        require = getattr(repository, "require_role", None)
        if require:
            if principal and principal.api_key_id:
                repository.assert_api_key_active(principal.api_key_id)
            require(principal, request.kb_ids, "read")

    def _binding_check(self, repository) -> None:
        # A retry may reuse its query vector only while the online binding is
        # unchanged. Never query a new generation with an old model silently.
        if not self.ranking_config or not hasattr(repository, "get_model_configuration"):
            return
        config = repository.get_model_configuration()
        if not config:
            raise SearchConfigurationChanged("model_configuration_removed")
        expected = {
            "revision": self.ranking_config.get("config_revision"),
            "active_index": self.ranking_config.get("active_generation"),
            "embedding_model": self.ranking_config.get("embedding_model"),
            "reranker_model": self.ranking_config.get("reranker_model"),
        }
        for key, value in expected.items():
            if value is not None and config.get(key) != value:
                raise SearchConfigurationChanged("model_configuration_changed")

    @staticmethod
    def _matches(chunk: Chunk, request: V2SearchRequest) -> bool:
        filters = request.filters
        return (
            (not filters.document_ids or chunk.document_id in filters.document_ids)
            and (not filters.category or chunk.metadata.get("category") == filters.category)
            and scope_matches(chunk, filters.product_model, filters.software_version)
        )

    @staticmethod
    def _valid_rows(rows, limit: int) -> list[tuple[UUID, float]]:
        result, seen = [], set()
        for item_id, score in rows:
            if not isinstance(item_id, UUID):
                item_id = UUID(str(item_id))
            if not isinstance(score, (int, float)) or isinstance(score, bool) or not math.isfinite(score):
                raise ValueError("candidate_score_not_finite")
            if item_id not in seen:
                seen.add(item_id)
                result.append((item_id, float(score)))
            if len(result) >= limit:
                break
        return result

    async def _recall(
        self, request: V2SearchRequest, state: _Attempt, execution: SearchExecution,
        query_vector: list[float] | None,
    ) -> list[float] | None:
        filters = {**request.filters.model_dump(), "_local_applicability": True}

        async def keyword():
            execution.record_call("keyword")
            with execution.stage("keyword"):
                return self._valid_rows(await self._invoke(
                    self.search, "keyword_search", request.query, request.kb_ids,
                    filters, state.keyword_limit, phrase=state.path == "exact",
                ), state.keyword_limit)

        async def vector():
            nonlocal query_vector
            if self.model is None:
                raise RuntimeError("model_client_unconfigured")
            if query_vector is None:
                execution.record_call("embedding")
                with execution.stage("embedding"):
                    values = await self._invoke(
                        self.model, "embed", [request.query],
                        execution.remaining_ms(self.settings.model_timeout_ms),
                    )
                if len(values) != 1 or not values[0] or any(
                    not isinstance(v, (float, int)) or isinstance(v, bool) or not math.isfinite(v)
                    for v in values[0]
                ):
                    raise ValueError("query_embedding_invalid")
                query_vector = list(values[0])
            execution.record_call("vector")
            with execution.stage("vector"):
                return self._valid_rows(await self._invoke(
                    self.search, "vector_search", query_vector, request.kb_ids,
                    filters, state.vector_limit,
                ), state.vector_limit)

        tasks = [asyncio.create_task(keyword())]
        if state.path != "exact":
            tasks.append(asyncio.create_task(vector()))
        try:
            values = await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        execution.checkpoint()
        for name, value in zip(("keyword", "vector"), values, strict=False):
            if isinstance(value, BaseException):
                if isinstance(value, (SearchDeadlineExceeded, asyncio.CancelledError)):
                    raise value
                if f"{name}_unavailable" not in state.degraded:
                    state.degraded.append(f"{name}_unavailable")
                state.skipped.append({"stage": name, "reason": "dependency_unavailable"})
                setattr(state, name, [])
                setattr(state, f"{name}_ok", False)
            else:
                setattr(state, name, value)
                setattr(state, f"{name}_ok", True)
                if name not in state.executed:
                    state.executed.append(name)
                if name == "vector" and "embedding" not in state.executed:
                    state.executed.append("embedding")
        if state.path == "exact":
            state.skipped.append({"stage": "embedding", "reason": "exact_path"})
        return query_vector

    def _preflight(self, request, principal, execution):
        """Reject unauthorized scopes before sending any query to a backend."""
        with self._snapshot() as repository, execution.stage("authority"):
            self._authorize(repository, request, principal)
            self._binding_check(repository)
            revisions = {}
            for kb_id in request.kb_ids:
                kb = repository.get_knowledge_base(kb_id)
                if kb is None:
                    raise KeyError("knowledge_base_not_found")
                revisions[str(kb_id)] = kb.content_revision
            return revisions

    def _candidate_snapshot(
        self, request, state: _Attempt, principal, execution: SearchExecution,
        discover_relations: bool,
    ) -> None:
        with self._snapshot() as repository, execution.stage("authority"):
            self._authorize(repository, request, principal)
            self._binding_check(repository)
            revisions = {}
            for kb_id in request.kb_ids:
                kb = repository.get_knowledge_base(kb_id)
                if kb is None:
                    raise KeyError("knowledge_base_not_found")
                revisions[str(kb_id)] = kb.content_revision
            if not state.revisions:
                state.revisions = revisions
            rankings = [[item for item, _ in state.keyword]]
            if state.path != "exact":
                rankings.append([item for item, _ in state.vector])
            ids = list(dict.fromkeys(item for ranking in rankings for item in ranking))
            legal = {
                chunk.id: chunk for chunk in repository.load_chunks(ids, request.kb_ids)
                if self._matches(chunk, request)
            }
            if state.path == "related" and discover_relations:
                state.seeds = list(legal)[:100]
                try:
                    execution.record_call("relation")
                    with execution.stage("relation"):
                        state.relations = repository.related_chunks(
                            request, state.seeds, state.relation_limit,
                            execution.remaining_ms(self.settings.relation_timeout_ms),
                        )[:state.relation_limit]
                    state.executed.append("relation")
                except SearchDeadlineExceeded:
                    raise
                except Exception as exc:
                    # Only the deliberately bounded relation stage may fail soft;
                    # a broken authoritative transaction remains a hard failure.
                    if getattr(getattr(exc, "orig", None), "sqlstate", None) != "57014":
                        raise
                    state.degraded.append("relation_unavailable")
            relation_ids = list(dict.fromkeys(row["chunk_id"] for row in state.relations))
            if relation_ids:
                rankings.append(relation_ids)
            with execution.stage("fusion"):
                state.scores = rrf(rankings)
                candidate_ids = sorted(state.scores, key=lambda item: (-state.scores[item], str(item)))
            chunks = repository.load_chunks(candidate_ids, request.kb_ids)
            by_id = {chunk.id: chunk for chunk in chunks}
            legal = {key: chunk for key, chunk in by_id.items() if self._matches(chunk, request)}
            state.counts.keyword = len(state.keyword)
            state.counts.vector = len(state.vector)
            state.counts.relation = len(relation_ids)
            state.counts.union = len(state.scores)
            state.counts.authoritative = len(legal)
            state.omissions.scope_excluded = len(by_id) - len(legal)
            state.omissions.stale_candidate = len(candidate_ids) - len(by_id)
            ordered = [item for item in candidate_ids if item in legal]
            with execution.stage("dedup"):
                state.ordered, removed, headings = self._dedup(ordered, legal)
            state.counts.duplicate_removed = removed
            state.omissions.heading_only = headings
            state.chunks = legal
            for stage in ("rrf", "dedup"):
                if stage not in state.executed:
                    state.executed.append(stage)
            execution.checkpoint()

    @staticmethod
    def _dedup(ordered: list[UUID], chunks: dict[UUID, Chunk]):
        seen, output, removed, headings = set(), [], 0, 0
        for item in ordered:
            chunk = chunks[item]
            structure = chunk.evidence_structure
            if structure.block_kind == "heading":
                headings += 1
                continue
            normalized = "\n".join(chunk.source_text.strip().splitlines())
            # Unknown scope may only collapse within the same explicit source unit.
            # Without that unit, every original chunk stays distinct.
            unit = None
            if structure.applicability.status != "resolved":
                unit = structure.source_unit_id or str(chunk.id)
            key = (
                chunk.kb_id, chunk.document_id, chunk.version_id, unit, normalized,
                json.dumps(structure.applicability.model_dump(mode="json"), sort_keys=True),
                json.dumps([link.model_dump(mode="json") for link in structure.context_links], sort_keys=True),
                structure.dependency_unresolved,
            )
            if key in seen:
                removed += 1
            else:
                seen.add(key)
                output.append(item)
        return output, removed, headings

    async def _rerank(self, request, state, execution):
        reason = (
            "exact_path" if state.path == "exact" else
            "reranker_service_unconfigured" if not self.reranker_configured else
            "rerank_disabled" if not self.settings.rerank_enabled else
            "model_client_unconfigured" if self.model is None else
            "not_enough_candidates" if len(state.ordered) < 2 else
            "insufficient_remaining_budget" if execution.remaining_ms() < self.settings.rerank_min_remaining_ms else
            None
        )
        if reason:
            state.reranker_skip = reason
            state.skipped.append({"stage": "rerank", "reason": reason})
            return
        window_size = state.rerank_window
        window = state.ordered[:window_size]
        execution.record_call("rerank")
        state.reranker_executed = True
        try:
            with execution.stage("rerank"):
                values = await self._invoke(
                    self.model, "rerank", request.query,
                    [build_search_text(state.chunks[item], state.chunks[item].document_name or "", template="evidence-v2") for item in window],
                    execution.remaining_ms(self.settings.model_timeout_ms),
                )
            if len(values) != len(window) or any(
                not isinstance(value, (float, int)) or isinstance(value, bool) or not math.isfinite(value)
                for value in values
            ):
                raise ValueError("reranker_score_not_finite_or_incomplete")
            state.rerank_scores = dict(zip(window, map(float, values), strict=True))
            state.omissions.rerank_window_limit = len(state.ordered) - len(window)
            state.ordered = sorted(window, key=lambda item: (-state.rerank_scores[item], str(item)))
            state.counts.reranked = len(window)
            state.executed.append("rerank")
        except SearchDeadlineExceeded:
            raise
        except Exception:
            execution.checkpoint()
            state.rerank_scores = {}
            state.degraded.append("rerank_unavailable")
            state.skipped.append({"stage": "rerank", "reason": "dependency_unavailable"})

    def _final_snapshot(self, request, state, principal, execution, retry_allowed):
        from cuekb.services.context import assemble_context_v2

        with self._snapshot() as repository, execution.stage("final_validation"):
            self._authorize(repository, request, principal)
            self._binding_check(repository)
            revisions = {}
            for kb_id in request.kb_ids:
                kb = repository.get_knowledge_base(kb_id)
                if kb is None:
                    raise KeyError("knowledge_base_not_found")
                revisions[str(kb_id)] = kb.content_revision
            if state.revisions != revisions and retry_allowed:
                return None, revisions
            if state.revisions != revisions:
                state.degraded.append("index_transition")
            current = {
                chunk.id: chunk for chunk in repository.load_chunks(state.ordered, request.kb_ids)
                if self._matches(chunk, request)
            }
            relations = []
            if state.relations:
                revalidate = getattr(repository, "revalidate_relations", None)
                if revalidate is None:
                    state.degraded.append("relation_unavailable")
                else:
                    relations = revalidate(
                        request, state.relations, state.seeds,
                        execution.remaining_ms(self.settings.relation_timeout_ms),
                    )
            relation_ids = {row["chunk_id"] for row in relations}
            direct = {item for item, _ in state.keyword + state.vector}
            ordered = [item for item in state.ordered if item in current and (item in direct or item in relation_ids)]
            if state.relations and not state.rerank_scores:
                # A removed/changed assertion cannot keep contributing a hidden
                # RRF bonus to a hit that also has a valid direct retrieval path.
                rankings = [[item for item, _ in state.keyword]]
                if state.path != "exact":
                    rankings.append([item for item, _ in state.vector])
                rankings.append(list(dict.fromkeys(row["chunk_id"] for row in relations)))
                state.scores = rrf(rankings)
                ordered.sort(key=lambda item: (-state.scores[item], str(item)))
            state.omissions.stale_candidate += len(state.ordered) - len(ordered)
            ordered = ordered[:request.top_k]
            plans = []
            remaining = self.settings.context_max_chars
            with execution.stage("context"):
                # All necessary groups are reserved before a single optional
                # neighbor can consume another hit's budget.
                for item in ordered:
                    execution.checkpoint()
                    chunk = current[item]
                    context_fn = getattr(repository, "context_chunks_v2", None)
                    if context_fn:
                        neighbors = context_fn(chunk, min(20, self.settings.context_max_chunks), include_optional=request.include_context)
                    else:
                        required = [link.chunk_id for link in chunk.evidence_structure.context_links]
                        neighbors = repository.load_chunks(required, request.kb_ids)
                        if request.include_context:
                            neighbors += repository.context_chunks(chunk, self.settings.context_max_chunks)
                    # Never depend on stale objects from candidate/reranker snapshots.
                    ids = list(dict.fromkeys(c.id for c in neighbors if c.id != chunk.id))[:20]
                    neighbors = repository.load_chunks(ids, request.kb_ids)
                    assembly = assemble_context_v2(
                        chunk, neighbors, min(remaining, self.settings.context_per_hit_chars),
                        include_optional=request.include_context, max_chunks=self.settings.context_max_chunks,
                        required_only=True,
                    )
                    remaining -= len(assembly.context)
                    hit = self._hit(chunk, state, request, relations, assembly, len(plans) + 1)
                    plans.append([hit, chunk, neighbors])
                hits = [plan[0] for plan in plans]
                maximum = min(request.max_evidence_bytes, self.settings.v2_max_evidence_bytes)
                while hits and len(encode_v2_hits(hits)) > maximum:
                    removed = plans.pop()
                    remaining += len(removed[0].context or "")
                    hits.pop()
                    state.omissions.payload_budget_exhausted += 1
                for index, (hit, chunk, neighbors) in enumerate(plans):
                    if not request.include_context:
                        continue
                    budget = min(self.settings.context_per_hit_chars, remaining + len(hit.context or ""))
                    assembly = assemble_context_v2(
                        chunk, neighbors, budget, include_optional=True,
                        max_chunks=self.settings.context_max_chunks, required_only=False,
                    )
                    expanded = self._hit(chunk, state, request, relations, assembly, hit.rank)
                    trial = hits.copy()
                    trial[index] = expanded
                    if len(encode_v2_hits(trial)) <= maximum:
                        remaining -= len(expanded.context or "") - len(hit.context or "")
                        hits[index] = expanded
                    elif expanded.context_parts != hit.context_parts:
                        hit.context_truncated = True
                        hit.context_state = "unresolved" if hit.context_state == "unresolved" else "truncated"
                        hit.context_omissions = list(dict.fromkeys([*hit.context_omissions, "budget_omitted"]))
                # State labels themselves cost bytes. Recheck the final exact
                # buffer after all changes; source facts are never sliced.
                while hits and len(encode_v2_hits(hits)) > maximum:
                    hits.pop()
                    state.omissions.payload_budget_exhausted += 1
                for rank, hit in enumerate(hits, 1):
                    hit.rank = rank
                state.omissions.dependency_unresolved += sum(hit.context_state == "unresolved" for hit in hits)
                state.omissions.scope_excluded += sum("unsafe_scope_excluded" in hit.context_omissions for hit in hits)
                state.omissions.context_budget_omitted += sum("budget_omitted" in hit.context_omissions for hit in hits)
            execution.checkpoint()
            return hits, revisions

    def _hit(self, chunk, state, request, relations, assembly, rank):
        stage_scores = {}
        if request.include_diagnostics:
            stage_scores["rrf"] = state.scores[chunk.id]
            for name, rows in (("keyword", state.keyword), ("vector", state.vector)):
                values = dict(rows)
                if chunk.id in values:
                    stage_scores[name] = values[chunk.id]
            if chunk.id in state.rerank_scores:
                stage_scores["rerank"] = state.rerank_scores[chunk.id]
        source_ids = {
            "keyword": {item for item, _ in state.keyword},
            "vector": {item for item, _ in state.vector},
            "relation": {row["chunk_id"] for row in relations},
        }
        return V2SearchHit(
            chunk_id=chunk.id, document_id=chunk.document_id, version_id=chunk.version_id,
            rank=rank, source_text=chunk.source_text, context=assembly.context or None,
            title_path=chunk.title_path, anchor=chunk.anchor, metadata=chunk.metadata,
            document_name=chunk.document_name, content_sha256=chunk.content_sha256,
            processing_profile=chunk.processing_profile,
            source_unit_id=chunk.evidence_structure.source_unit_id,
            block_kind=chunk.evidence_structure.block_kind,
            score=state.rerank_scores.get(chunk.id, state.scores[chunk.id]),
            score_type="rerank" if state.rerank_scores else "rrf", stage_scores=stage_scores,
            applicability=chunk.evidence_structure.applicability,
            source_spans=chunk.source_spans, context_state=assembly.state,
            context_omissions=assembly.omissions, context_truncated=assembly.truncated,
            context_parts=[V2ContextPart.model_validate(part.model_dump()) for part in assembly.parts],
            relations=[
                {key: value for key, value in row.items() if key in V2RelationEvidence.model_fields}
                for row in relations if row["chunk_id"] == chunk.id
            ],
            retrieval_sources=[name for name, ids in source_ids.items() if chunk.id in ids],
        )

    def _ranking_version(self, state):
        # Explicit allowlist: no API key, endpoint URL, raw query or document body.
        binding_keys = (
            "config_revision", "embedding_model", "reranker_model", "embedding_revision",
            "reranker_revision", "embedding_input_revision", "rerank_input_revision", "lexical_revision",
        )
        value = {
            "algorithm_revision": ALGORITHM_REVISION, "rrf_constant": 60,
            "weights": [1, 1, 1], "keyword_limit": state.keyword_limit,
            "vector_limit": state.vector_limit, "relation_limit": state.relation_limit,
            "rerank_window": state.rerank_window,
            "field_boosts": {"source_text": 3, "title_path": 1, "document_name": 0.25},
            "dedup": "exact-scope-document-version-dependencies-v1",
            "selection": "required-groups-before-optional-v1",
            "rerank_enabled": self.settings.rerank_enabled,
            "reranker_configured": self.reranker_configured,
            "context_max_chunks": self.settings.context_max_chunks,
            "context_per_hit_chars": self.settings.context_per_hit_chars,
            "context_max_chars": self.settings.context_max_chars,
            "binding": {key: self.ranking_config[key] for key in binding_keys if key in self.ranking_config},
        }
        # Fixture bindings still identify their actual model, when available.
        value["binding"].setdefault("embedding_model", getattr(self.model, "embedding_model", None))
        value["binding"].setdefault("reranker_model", getattr(self.model, "_reranker_model", None))
        return "sha256:" + hashlib.sha256(_encode(value)).hexdigest()

    @staticmethod
    def _sizes(hits):
        source = "".join(hit.source_text for hit in hits)
        context = "".join(hit.context or "" for hit in hits)
        parts = "".join(part.source_text for hit in hits for part in hit.context_parts)
        return V2PayloadSizes(
            source_text_chars=len(source), source_text_bytes=len(source.encode("utf-8")),
            context_chars=len(context), context_bytes=len(context.encode("utf-8")),
            context_parts_chars=len(parts), context_parts_bytes=len(parts.encode("utf-8")),
            hits_bytes=len(encode_v2_hits(hits)),
        )

    async def _run(self, request, principal, execution):
        path, route_reason = self.router.route(request)
        query_vector = None
        for attempt in range(2):
            execution.checkpoint()
            state = _Attempt(
                path=path, route_reason=route_reason,
                keyword_limit=min(100, self.settings.keyword_candidates),
                vector_limit=min(100, self.settings.vector_candidates) if path != "exact" else 0,
                relation_limit=min(50, self.settings.relation_candidates) if path == "related" else 0,
                skipped=[{"stage": "route", "reason": route_reason}],
                rerank_window=min(100, max(request.top_k, self.settings.rerank_candidates)),
            )
            state.revisions = await self._read(lambda: self._preflight(request, principal, execution))
            query_vector = await self._recall(request, state, execution, query_vector)
            await self._read(lambda: self._candidate_snapshot(request, state, principal, execution, True))
            filtered = state.counts.union - len(state.ordered)
            can_refill = (
                not execution.refill_used and filtered > 0 and len(state.ordered) < request.top_k
                and execution.remaining_ms() >= self.settings.v2_refill_min_remaining_ms
                and ((state.keyword_ok and state.keyword_limit < 100)
                     or (state.vector_ok and state.vector_limit < 100))
            )
            if can_refill:
                execution.refill_used = True
                state.keyword_limit = min(100, state.keyword_limit * 2)
                if state.vector_limit:
                    state.vector_limit = min(100, state.vector_limit * 2)
                query_vector = await self._recall(request, state, execution, query_vector)
                await self._read(lambda: self._candidate_snapshot(request, state, principal, execution, False))
            await self._rerank(request, state, execution)
            hits, revisions = await self._read(lambda: self._final_snapshot(
                request, state, principal, execution, retry_allowed=attempt == 0,
            ))
            if hits is None:
                continue
            state.counts.returned = len(hits)
            if not hits and (state.omissions.payload_budget_exhausted or state.omissions.scope_excluded or state.omissions.heading_only):
                state.degraded.append("evidence_selection_limited")
            degraded = list(dict.fromkeys(state.degraded))
            status = RetrievalStatus.DEGRADED if degraded else RetrievalStatus.OK if hits else RetrievalStatus.NOT_FOUND
            method = (
                "rrf_then_rerank" if state.rerank_scores else
                "rrf_related" if path == "related" and "relation" in state.executed else
                "keyword_rrf" if path == "exact" or not state.vector_ok else "rrf"
            )
            execution.timings_ms["total"] = (perf_counter() - execution.started) * 1000
            execution.checkpoint()
            return V2SearchResponse(
                trace_id=execution.trace_id, request_id=request.request_id,
                retrieval_status=status, evidence_status=EvidenceStatus.UNASSESSED,
                degraded_reasons=degraded, scope_limited=path == "related",
                content_revisions=revisions, timings_ms=dict(execution.timings_ms),
                retrieval_path=path, executed_stages=state.executed, skipped_stages=state.skipped,
                ranking_method=method, ranking_version=self._ranking_version(state), hits=hits,
                diagnostics=V2SearchDiagnostics(
                    counts=state.counts, omissions=state.omissions, sizes=self._sizes(hits),
                    backend_calls=V2BackendCalls(**execution.backend_calls), attempt_count=attempt + 1,
                    reranker=V2RerankerDiagnostics(
                        configured=self.reranker_configured, executed=state.reranker_executed,
                        skipped_reason=state.reranker_skip,
                    ), active_generation=self.ranking_config.get("active_generation") or getattr(self.search, "index_name", None),
                    processing_profiles=sorted({hit.processing_profile for hit in hits}),
                    keyword_limit=state.keyword_limit, vector_limit=state.vector_limit,
                    relation_limit=state.relation_limit,
                ),
            )
        raise RuntimeError("unreachable_version_retry")
