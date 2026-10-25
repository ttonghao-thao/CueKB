"""Fixture checks for V2 risks, not model-quality or production acceptance."""
import asyncio
import json
from contextlib import contextmanager
from uuid import uuid4

import pytest
from pydantic import ValidationError

from cuekb.adapters.memory import InMemoryRepository
from cuekb.config import Settings
from cuekb.domain.models import (
    Applicability, ApplicableSubject, Chunk, ContextLink, Document, DocumentVersion,
    EvidenceStructure, KnowledgeBase, RetrievalStatus, VersionStatus,
)
from cuekb.schemas import V2SearchHit, V2SearchRequest
from cuekb.services.execution import SearchDeadlineExceeded, SearchExecution
from cuekb.services.retrieval_v2 import V2RetrievalService, encode_v2_hits, encode_v2_response


class FixtureSearch:
    def __init__(self, keyword, vector=None):
        self.keyword = keyword
        self.vector = vector if vector is not None else keyword
        self.keyword_limits, self.vector_limits = [], []

    async def keyword_search_async(self, query, kb_ids, filters, limit, phrase=False):
        self.keyword_limits.append(limit)
        return self.keyword[:limit]

    async def vector_search_async(self, vector, kb_ids, filters, limit):
        self.vector_limits.append(limit)
        return self.vector[:limit]


class FixtureModel:
    embedding_model = "fixture-embedding"

    def __init__(self, rerank=None):
        self.embedding_calls = 0
        self.rerank_calls = 0
        self.scores = rerank

    async def embed_async(self, texts, timeout_ms):
        self.embedding_calls += 1
        return [[0.1, 0.2]]

    async def rerank_async(self, query, passages, timeout_ms):
        self.rerank_calls += 1
        return self.scores if self.scores is not None else list(range(len(passages)))


def fixture(count=3, texts=None, repository=None, **settings):
    repository = repository or InMemoryRepository()
    kb = repository.create_knowledge_base(KnowledgeBase(name="fixed-fixture"))
    doc = Document(kb_id=kb.id, name="two products")
    version = DocumentVersion(document_id=doc.id, content_sha256="a" * 64, status=VersionStatus.PUBLISHED)
    repository.documents[doc.id] = doc
    repository.versions[version.id] = version
    subject = ApplicableSubject(display_name="Black Pig", product_model="BP-1", provenance_id="approved:1")
    chunks = [Chunk(
        kb_id=kb.id, document_id=doc.id, version_id=version.id, ordinal=index,
        source_text=texts[index] if texts else f"Fact {index}: sealed 45 days; opened refrigerate 3 days.",
        search_text=f"Black Pig fact {index}", title_path=["Black Pig"],
        processing_profile="evidence-v2:fixture",
        evidence_structure=EvidenceStructure(
            source_unit_id=f"unit:{index}", applicability=Applicability(status="resolved", subjects=[subject]),
        ),
    ) for index in range(count)]
    repository.chunks.update({chunk.id: chunk for chunk in chunks})
    rows = [(chunk.id, float(count - index)) for index, chunk in enumerate(chunks)]
    search = FixtureSearch(rows)
    model = FixtureModel()
    service = V2RetrievalService(repository, search, Settings(**settings), model=model)
    request = V2SearchRequest(query="storage conditions and weight", kb_ids=[kb.id], include_context=False)
    return repository, chunks, search, model, service, request


def test_unconfigured_reranker_is_healthy_with_real_rrf_scores():
    _, chunks, _, model, service, request = fixture()
    result = service.search_evidence(request)
    assert result.retrieval_status == RetrievalStatus.OK
    assert result.degraded_reasons == []
    assert result.ranking_method == "rrf"
    assert result.hits[0].chunk_id == chunks[0].id
    assert result.hits[0].score == pytest.approx(2 / 61)
    assert [hit.rank for hit in result.hits] == [1, 2, 3]
    assert model.rerank_calls == 0
    assert result.diagnostics.reranker.skipped_reason == "reranker_service_unconfigured"
    assert result.evidence_status == "unassessed"
    assert all(hit.stage_scores == {} for hit in result.hits)


@pytest.mark.parametrize("selection_mode", ["ranked", "adaptive"])
def test_ten_overlapping_queries_keep_scope_trace_and_counts_isolated(selection_mode):
    async def run():
        repo = InMemoryRepository()
        requests, expected = [], []
        for _ in range(10):
            _, chunks, _, _, _, request = fixture(count=1, repository=repo)
            request.top_k = 1
            request.selection_mode = selection_mode
            requests.append(request)
            expected.append(chunks[0].id)
        ready = asyncio.Event()
        active, maximum = 0, 0
        class MixedBackend:
            async def keyword_search_async(self, *args, **kwargs):
                nonlocal active, maximum
                active += 1
                maximum = max(maximum, active)
                if active == 10:
                    ready.set()
                await ready.wait()
                active -= 1
                # Deliberately include other scopes: authoritative reads must reject them.
                return [(item, 1.0) for item in expected]
            async def vector_search_async(self, *args, **kwargs):
                await ready.wait()
                return [(item, 1.0) for item in expected]
        service = V2RetrievalService(repo, MixedBackend(), Settings(), FixtureModel())
        results = await asyncio.gather(*(service.search_evidence_async(request) for request in requests))
        assert maximum == 10
        assert len({result.trace_id for result in results}) == 10
        for result, chunk_id in zip(results, expected):
            assert [hit.chunk_id for hit in result.hits] == [chunk_id]
            assert result.diagnostics.backend_calls.keyword == 1
            assert result.diagnostics.backend_calls.embedding == 1
    asyncio.run(run())


def test_rerank_window_includes_at_least_topk_and_never_mixes_rrf_tail():
    _, _, _, model, service, request = fixture(count=8, rerank_candidates=2)
    service.reranker_configured = True
    request.top_k = 4
    result = service.search_evidence(request)
    assert result.ranking_method == "rrf_then_rerank"
    assert result.diagnostics.counts.reranked == 4
    assert result.diagnostics.omissions.rerank_window_limit == 4
    assert [hit.score for hit in result.hits] == [3, 2, 1, 0]
    assert all(hit.score_type == "rerank" for hit in result.hits)
    assert model.rerank_calls == 1


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), True])
def test_bad_rerank_falls_back_as_one_complete_rrf_ranking(bad):
    _, _, _, model, service, request = fixture()
    service.reranker_configured = True
    model.scores = [1.0, bad, 0.1]
    result = service.search_evidence(request)
    assert result.ranking_method == "rrf"
    assert result.degraded_reasons == ["rerank_unavailable"]
    assert all(hit.score_type == "rrf" for hit in result.hits)
    assert result.hits[0].score == pytest.approx(2 / 61)
    json.loads(encode_v2_response(result))


def test_required_condition_survives_include_context_false():
    repository, chunks, search, _, service, request = fixture(count=2)
    fact, condition = chunks
    fact.evidence_structure.context_links = [ContextLink(chunk_id=condition.id, role="condition", required=True)]
    search.keyword = [(fact.id, 10)]
    search.vector = [(fact.id, 9)]
    result = service.search_evidence(request)
    hit = result.hits[0]
    assert hit.context_state == "complete"
    assert {part.chunk_id for part in hit.context_parts} == {fact.id, condition.id}
    assert all(part.required for part in hit.context_parts)
    assert hit.source_text == repository.chunks[fact.id].source_text


def test_missing_required_condition_is_unresolved_even_without_optional_context():
    _, chunks, _, _, service, request = fixture(count=1)
    chunks[0].evidence_structure.context_links = [ContextLink(chunk_id=uuid4(), role="condition")]
    result = service.search_evidence(request)
    assert result.hits[0].context_state == "unresolved"
    assert "dependency_unresolved" in result.hits[0].context_omissions


def test_local_scope_overrides_document_product_tag():
    _, chunks, _, _, service, request = fixture(count=1)
    chunks[0].metadata["product_model"] = "WRONG-DOCUMENT-TAG"
    request.filters.product_model = "WRONG-DOCUMENT-TAG"
    result = service.search_evidence(request)
    assert result.hits == []
    assert result.retrieval_status == "degraded"
    assert result.diagnostics.omissions.scope_excluded == 1
    request.filters.product_model = "BP-1"
    assert service.search_evidence(request).hits


def test_utf8_payload_limit_uses_final_json_hits_not_character_count():
    _, _, _, _, service, request = fixture(count=3, texts=["储存45天。" * 100 + str(index) for index in range(3)])
    request.max_evidence_bytes = 4096
    result = service.search_evidence(request)
    assert len(encode_v2_hits(result.hits)) <= 4096
    assert result.diagnostics.sizes.hits_bytes == len(encode_v2_hits(result.hits))
    assert result.diagnostics.omissions.payload_budget_exhausted > 0
    assert result.hits[0].source_text == "储存45天。" * 100 + "0"


def test_oversized_single_fact_is_selection_failure_not_not_found():
    _, _, _, _, service, request = fixture(count=1, texts=["x" * 8000])
    request.max_evidence_bytes = 4096
    result = service.search_evidence(request)
    assert result.hits == []
    assert result.retrieval_status == "degraded"
    assert result.diagnostics.omissions.payload_budget_exhausted == 1
    assert result.diagnostics.sizes.hits_bytes == 2


def test_refill_replaces_each_channel_and_reuses_query_embedding():
    _, chunks, search, model, service, request = fixture(count=1, keyword_candidates=1, vector_candidates=1)
    stale = uuid4()
    good = chunks[0].id

    async def keyword(query, kb_ids, filters, limit, phrase=False):
        search.keyword_limits.append(limit)
        return [(stale, 10)] if limit == 1 else [(good, 11), (stale, 10)]

    async def vector(vector, kb_ids, filters, limit):
        search.vector_limits.append(limit)
        return [(stale, 10)] if limit == 1 else [(good, 11), (stale, 10)]

    search.keyword_search_async = keyword
    search.vector_search_async = vector
    result = service.search_evidence(request)
    assert result.hits[0].score == pytest.approx(2 / 61)
    assert search.keyword_limits == [1, 2]
    assert search.vector_limits == [1, 2]
    assert model.embedding_calls == 1
    assert result.diagnostics.backend_calls.keyword == 2
    assert result.diagnostics.counts.union == 2


def test_identical_text_with_different_product_or_conditions_is_not_deduplicated():
    _, chunks, _, _, service, request = fixture(count=2, texts=["45 days.", "45 days."])
    chunks[1].evidence_structure.applicability.subjects[0] = ApplicableSubject(display_name="Golden Yolk")
    result = service.search_evidence(request)
    assert len(result.hits) == 2
    assert result.diagnostics.counts.duplicate_removed == 0


def test_native_deadline_cancels_and_awaits_in_flight_remote_call():
    _, _, search, _, service, request = fixture()
    cancelled = []

    async def slow(*args, **kwargs):
        try:
            await asyncio.sleep(10)
        finally:
            cancelled.append(True)

    search.keyword_search_async = slow
    with pytest.raises(SearchDeadlineExceeded):
        service.search_evidence(request, execution=SearchExecution.create(20))
    assert cancelled == [True]


def test_dependency_failure_with_zero_hits_is_degraded():
    _, _, search, _, service, request = fixture()

    async def failed(*args, **kwargs):
        raise RuntimeError("dependency unavailable")

    search.keyword_search_async = failed
    search.vector_search_async = failed
    result = service.search_evidence(request)
    assert result.hits == []
    assert result.retrieval_status == "degraded"
    assert set(result.degraded_reasons) == {"keyword_unavailable", "vector_unavailable"}


def test_normal_empty_lists_are_not_found():
    _, _, search, _, service, request = fixture()
    search.keyword = search.vector = []
    result = service.search_evidence(request)
    assert result.retrieval_status == "not_found"
    assert result.degraded_reasons == []


def test_final_relation_removal_also_removes_its_rrf_bonus():
    repo, chunks, _, _, service, request = fixture(count=2)
    request.mode = "related"
    relation = {"chunk_id": chunks[1].id, "relation_id": uuid4(),
                "subject_id": uuid4(), "object_id": uuid4(), "relation_type": "references",
                "conditions": {}, "stance": "supports"}
    repo.related_chunks = lambda *args: [relation]
    repo.revalidate_relations = lambda *args: []
    result = service.search_evidence(request)
    assert [hit.chunk_id for hit in result.hits] == [chunks[0].id, chunks[1].id]
    assert result.hits[1].score == pytest.approx(2 / 62)
    assert all(hit.relations == [] and "relation" not in hit.retrieval_sources for hit in result.hits)


def test_schema_is_closed_and_score_must_be_finite():
    _, _, _, _, service, request = fixture(count=1)
    hit = service.search_evidence(request).hits[0].model_dump()
    with pytest.raises(ValidationError):
        V2SearchHit(**{**hit, "score": float("nan")})
    with pytest.raises(ValidationError):
        V2SearchRequest(query="price", kb_ids=request.kb_ids, original_question="unagreed")


class TransitionRepository(InMemoryRepository):
    def __init__(self):
        super().__init__()
        self.snapshots = 0

    @contextmanager
    def read_snapshot(self):
        self.snapshots += 1
        if self.snapshots == 3:
            kb = next(iter(self.knowledge_bases.values()))
            kb.content_revision += 1
        yield self


def test_version_retry_is_bounded_and_keeps_one_request_budget_and_vector():
    repository, _, _, model, service, request = fixture(repository=TransitionRepository())
    result = service.search_evidence(request)
    assert result.diagnostics.attempt_count == 2
    assert result.diagnostics.backend_calls.keyword == 2
    assert model.embedding_calls == 1
    assert repository.snapshots == 6
    assert result.content_revisions == {str(request.kb_ids[0]): 1}


def test_unauthorized_request_never_starts_remote_calls():
    repository, _, search, model, service, request = fixture()

    def forbidden(*args):
        raise PermissionError("knowledge_base_access_denied")

    repository.require_role = forbidden
    with pytest.raises(PermissionError):
        service.search_evidence(request)
    assert search.keyword_limits == []
    assert model.embedding_calls == 0


def test_final_snapshot_drops_deleted_fact_after_rerank_without_rrf_tail():
    repository, chunks, _, model, service, request = fixture(count=6, rerank_candidates=2)
    service.reranker_configured = True
    request.top_k = 2

    async def rerank(query, passages, timeout_ms):
        model.rerank_calls += 1
        del repository.chunks[chunks[1].id]
        return [0.1, 0.9]

    model.rerank_async = rerank
    result = service.search_evidence(request)
    assert [hit.chunk_id for hit in result.hits] == [chunks[0].id]
    assert all(hit.score_type == "rerank" for hit in result.hits)
    assert model.rerank_calls == 1
    assert result.diagnostics.omissions.stale_candidate >= 1


def test_final_snapshot_reloads_dependency_body_and_span_after_model_io():
    repository, chunks, search, model, service, request = fixture(count=3)
    fact, second, dependency = chunks
    fact.evidence_structure.context_links = [ContextLink(chunk_id=dependency.id, role="condition")]
    search.keyword = [(fact.id, 3), (second.id, 2)]
    search.vector = search.keyword
    service.reranker_configured = True

    async def rerank(query, passages, timeout_ms):
        repository.chunks[dependency.id] = dependency.model_copy(update={"source_text": "Current opened condition: 3 days."})
        return [1.0, 0.1]

    model.rerank_async = rerank
    result = service.search_evidence(request)
    parts = {part.chunk_id: part for part in result.hits[0].context_parts}
    assert parts[dependency.id].source_text == "Current opened condition: 3 days."


def test_requested_diagnostics_are_finite_original_stage_scores():
    _, _, _, _, service, request = fixture(count=1)
    request.include_diagnostics = True
    hit = service.search_evidence(request).hits[0]
    assert hit.stage_scores == {"keyword": 1.0, "vector": 1.0, "rrf": pytest.approx(2 / 61)}


def test_ranking_digest_excludes_secrets_and_changes_with_actual_window():
    _, _, _, _, service, request = fixture(count=3, rerank_candidates=1)
    service.ranking_config = {"api_key": "private", "service_url": "https://private.example"}
    request.top_k = 1
    first = service.search_evidence(request)
    service.ranking_config = {"api_key": "different", "service_url": "https://different.example"}
    assert service.search_evidence(request).ranking_version == first.ranking_version
    request.top_k = 3
    assert service.search_evidence(request).ranking_version != first.ranking_version
