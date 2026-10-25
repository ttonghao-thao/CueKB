"""Controlled evidence/contract regressions, not a real-model accuracy benchmark."""
import importlib.util
import json
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from cuekb.domain.models import ApplicableSubject, ContextLink
from cuekb.schemas import V2SearchRequest, V2SearchResponse
from cuekb.services.retrieval_v2 import encode_v2_hits, encode_v2_response
from cuekb.services.selection import (
    CalibrationStats, RelevanceProfile, identifiers, runtime_binding, tokens,
)
from test_retrieval_v2 import fixture


@pytest.mark.parametrize("mode", ["ranked", "adaptive"])
def test_budget_skips_large_candidate_and_returns_next_usable_fact(mode):
    _, chunks, _, _, service, request = fixture(count=2, texts=["x" * 8000, "Input: 12 V."])
    request.top_k, request.max_evidence_bytes, request.selection_mode = 1, 4096, mode
    result = service.search_evidence(request)
    assert [hit.chunk_id for hit in result.hits] == [chunks[1].id]
    assert len(encode_v2_hits(result.hits)) <= 4096
    assert result.diagnostics.omissions.payload_budget_exhausted == 1


def test_adaptive_selection_covers_attributes_without_filling_limit_with_introductions():
    texts = ["Product overview: exceptional craftsmanship.", "Product overview: exceptional craftsmanship and care.",
             "Weight: 500 g.", "Price: 20 USD.", "Storage: 3 days after opening."]
    _, chunks, _, _, service, request = fixture(count=len(texts), texts=texts)
    request.selection_mode, request.query, request.query_facets = "adaptive", "price weight storage", ["price", "weight", "storage"]
    request.top_k = 20
    result = service.search_evidence(request)
    assert [h.chunk_id for h in result.hits] == [c.id for c in chunks[2:]]
    assert [h.rank for h in result.hits] == [1, 2, 3]
    assert result.diagnostics.selection.stop_reason == "no_information_gain"
    assert result.diagnostics.selection.observed_facet_indices == [0, 1, 2]
    assert result.diagnostics.selection.coverage_assessment == "unassessed"
    assert result.evidence_status == "unassessed"
    assert [h.score for h in result.hits] == sorted([h.score for h in result.hits], reverse=True)
    # Unconfigured reranker is a healthy adaptive path, not a degradation.
    assert result.degraded_reasons == []


def test_adaptive_optional_neighbors_require_new_information_not_just_proximity():
    _, chunks, _, _, service, request = fixture(count=3, texts=[
        "Product overview: exceptional craftsmanship.", "Weight: 500 g.", "Weight: 600 g. after cooling."])
    request.query, request.selection_mode, request.query_facets = "weight", "adaptive", ["weight"]
    request.include_context = True
    result = service.search_evidence(request)
    returned = {part.chunk_id for hit in result.hits for part in hit.context_parts}
    assert chunks[0].id not in returned
    assert {h.chunk_id for h in result.hits} == {chunks[1].id, chunks[2].id}


def test_adaptive_keeps_numeric_condition_negation_and_product_differences():
    texts = ["Storage: 45 days.", "Storage: 3 days after opening.", "Storage: not permitted when opened.",
             "Storage: 45 days.", "Storage: 45 days if refrigerated."]
    _, chunks, _, _, service, request = fixture(count=5, texts=texts)
    chunks[3].evidence_structure.applicability.subjects = [ApplicableSubject(display_name="Other Product")]
    request.query, request.selection_mode, request.query_facets = "storage", "adaptive", ["storage"]
    result = service.search_evidence(request)
    assert {h.chunk_id for h in result.hits} == {c.id for c in chunks}


def test_distinct_prohibitions_survive_even_without_repeating_the_attribute_name():
    _, chunks, _, _, service, request = fixture(count=3, texts=["Storage: 3 days.", "Do not freeze.", "Do not refrigerate."])
    request.query, request.selection_mode, request.query_facets = "storage", "adaptive", ["storage"]
    assert {h.chunk_id for h in service.search_evidence(request).hits} == {c.id for c in chunks}


def test_multilingual_tokens_ratios_signs_and_units_cannot_be_collapsed():
    assert tokens("Größe Café хранение 储存") == {"grösse", "café", "хранение", "储", "存"}
    texts = ["Ratio: 2:1", "Ratio: 1:2", "Ratio: -20 °C", "Ratio: 20 °C", "Ratio: 12 公斤", "Ratio: 12 克"]
    _, chunks, _, _, service, request = fixture(count=6, texts=texts)
    request.query, request.selection_mode, request.query_facets = "ratio", "adaptive", ["ratio"]
    assert {h.chunk_id for h in service.search_evidence(request).hits} == {c.id for c in chunks}


@pytest.mark.parametrize("minus", ["−", "﹣", "－"])
def test_unicode_negative_values_remain_distinct_from_positive_values(minus):
    _, chunks, _, _, service, request = fixture(count=2, texts=[
        f"Temperature: {minus}20 °C", "Temperature: 20 °C"])
    request.query, request.selection_mode, request.query_facets = "temperature", "adaptive", ["temperature"]
    result = service.search_evidence(request)
    assert {hit.chunk_id for hit in result.hits} == {chunk.id for chunk in chunks}


def test_optional_reranker_gate_uses_real_negative_logits_and_keeps_rrf_tail_out():
    _, _, _, model, service, request = fixture(count=3)
    service.reranker_configured = True
    model.scores = [-100., -101., -102.]
    stats = CalibrationStats(dataset_sha256="c" * 64, positives=5, negatives=5, families=2,
        target_false_positive_rate=.05, observed_false_positive_rate=0, observed_recall=1)
    service.relevance_profile = RelevanceProfile(binding=runtime_binding({}, model),
        rerank_min_score=-100.5, calibration={"rerank": stats})
    request.selection_mode = "adaptive"
    result = service.search_evidence(request)
    assert [h.score for h in result.hits] == [-100.]
    assert all(h.score_type == "rerank" for h in result.hits)
    assert result.diagnostics.selection.assessed == 3 and result.diagnostics.selection.rejected == 2
    assert result.evidence_status == "unassessed"


def test_required_context_does_not_consume_another_result_slot_or_lose_its_identity():
    _, chunks, _, _, service, request = fixture(count=2, texts=["Storage: 45 days.", "Only when sealed; opened storage is 3 days."])
    chunks[0].evidence_structure.context_links = [ContextLink(chunk_id=chunks[1].id, role="condition")]
    request.query, request.selection_mode, request.query_facets = "storage", "adaptive", ["storage"]
    result = service.search_evidence(request)
    assert len(result.hits) == 1
    assert {part.chunk_id for part in result.hits[0].context_parts} == {c.id for c in chunks}
    assert result.hits[0].context_state == "complete"


def test_calibrated_gate_abstains_and_never_relabels_evidence_sufficient(tmp_path):
    _, chunks, search, model, service, request = fixture(count=2)
    request.selection_mode = "adaptive"
    stats = CalibrationStats(dataset_sha256="a" * 64, positives=5, negatives=5, families=2,
        target_false_positive_rate=.05, observed_false_positive_rate=0, observed_recall=1)
    profile = RelevanceProfile(binding=runtime_binding({}, model), vector_min_score=.8, calibration={"vector": stats})
    path = tmp_path / "profile.json"
    path.write_text(profile.model_dump_json())
    service = type(service)(service.repository, search, service.settings.model_copy(update={
        "v2_relevance_profile_path": str(path)}), model)
    search.keyword = []
    search.vector = [(chunk.id, .1) for chunk in chunks]
    result = service.search_evidence(request)
    assert result.hits == [] and result.retrieval_status == "not_found"
    assert result.diagnostics.selection.rejected == 2
    assert result.diagnostics.selection.assessed == 2
    assert result.diagnostics.selection.calibration_status == "applied"
    assert result.evidence_status == "unassessed"
    service.ranking_config = {"config_revision": 2}
    result = service.search_evidence(request)
    assert result.diagnostics.selection.calibration_status == "binding_mismatch"
    assert result.diagnostics.selection.assessed == 0
    assert result.hits and result.evidence_status == "unassessed"


def test_adaptive_identifier_guard_works_on_both_recall_and_final_authority():
    _, chunks, _, _, service, request = fixture(count=2, texts=["A-100 input: 12 V", "A-1000 input: 24 V"])
    request.query, request.selection_mode = "A-100 input", "adaptive"
    result = service.search_evidence(request)
    assert [h.chunk_id for h in result.hits] == [chunks[0].id]
    assert identifiers("12 V input for A-100") == {"a-100"}


def test_missing_facet_refill_is_once_and_reuses_original_embedding():
    _, chunks, search, model, service, request = fixture(count=2, texts=["Price: 20 USD", "Weight: 500 g"],
        keyword_candidates=1, vector_candidates=1)
    request.query, request.selection_mode, request.query_facets, request.top_k = "price weight", "adaptive", ["price", "weight"], 1
    result = service.search_evidence(request)
    assert search.keyword_limits == [1, 2]
    assert model.embedding_calls == 1
    assert result.diagnostics.backend_calls.keyword == 2
    assert any(s["reason"] == "missing_literal_facet" for s in result.skipped_stages)


def test_ranked_wire_shape_stays_closed_and_adaptive_diagnostics_are_explicit():
    _, _, _, _, service, request = fixture(count=1)
    baseline = json.loads(encode_v2_response(service.search_evidence(request)))
    assert "selection" not in baseline["diagnostics"]
    request.selection_mode = "adaptive"
    adaptive = json.loads(encode_v2_response(service.search_evidence(request)))
    assert adaptive["diagnostics"]["selection"]["version"] == "evidence-set-v1"
    V2SearchResponse.model_validate(adaptive)
    with pytest.raises(ValidationError):
        V2SearchRequest(query="price", kb_ids=request.kb_ids, query_facets=["price"])


def test_calibration_refuses_mixed_models_leaked_families_and_nonseparable_scores():
    path = Path(__file__).parents[1] / "scripts/calibrate-retrieval.py"
    spec = importlib.util.spec_from_file_location("cuekb_calibration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    binding = runtime_binding({}, None)
    records = [module.ScoreRecord(sample_id=str(i), family=str(i % 2), split="calibration", channel="vector",
        score=.9 if i < 5 else .1, relevant=i < 5, binding=binding) for i in range(10)]
    profile = module.fit(records, .05, "b" * 64)
    assert profile.vector_min_score == .9
    mixed = records.copy()
    mixed[-1] = mixed[-1].model_copy(update={"binding": binding.model_copy(update={"embedding_model": "other"})})
    with pytest.raises(ValueError, match="mixed_runtime_bindings"):
        module.fit(mixed, .05, "b" * 64)
    inseparable = [row.model_copy(update={"score": .5}) for row in records]
    with pytest.raises(ValueError, match="no_usable_threshold"):
        module.fit(inseparable, .05, "b" * 64)
    records[-1] = records[-1].model_copy(update={"split": "test"})
    with pytest.raises(ValueError, match="family_leakage"):
        module.fit(records, .05, "b" * 64)


def test_relation_candidates_rank_by_query_evidence_before_uuid_and_have_bounded_bonus():
    repo, chunks, search, _, service, request = fixture(count=3, texts=["Storage: 45 days", "Factory overview", "Storage: refrigerate after opening"])
    request.mode = "related"
    search.keyword = [(chunks[0].id, 1)]
    search.vector = search.keyword
    rows = [{"chunk_id": chunk.id, "relation_id": uuid4(), "subject_id": uuid4(), "object_id": uuid4(),
             "relation_type": "references", "conditions": {}, "stance": "supports"} for chunk in chunks[1:]]
    repo.related_chunks = lambda *args: rows
    repo.revalidate_relations = lambda *args: rows
    request.query = "storage refrigerate"
    result = service.search_evidence(request)
    assert [h.chunk_id for h in result.hits] == [chunks[0].id, chunks[2].id, chunks[1].id]
    assert result.hits[1].score == pytest.approx(.5 / 61)


def test_multi_evidence_evaluation_rejects_incomplete_context_and_accepts_alternative_plan():
    path = Path(__file__).parents[1] / "scripts/evaluate-retrieval.py"
    spec = importlib.util.spec_from_file_location("cuekb_evaluation_v2", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    case = {"gold_evidence": [{"unit_id": "fact", "chunk_ids": ["f"]},
                               {"unit_id": "condition", "chunk_ids": ["c"]},
                               {"unit_id": "alternative", "chunk_ids": ["a"]}],
            "gold_plans": [{"unit_ids": ["fact", "condition"]}, {"unit_ids": ["alternative"]}], "answerable": True}
    partial = {"hits": [{"chunk_id": "f", "context_parts": [], "context_state": "unresolved"}]}
    assert module.evidence_metrics(case, partial)["complete"] is False
    complete = {"hits": [{"chunk_id": "f", "context_parts": [{"chunk_id": "c"}], "context_state": "complete"}]}
    assert module.evidence_metrics(case, complete)["complete"] is True
    assert module.evidence_metrics(case, {"hits": [{"chunk_id": "a", "context_parts": []}]})["complete"] is True
    assert module.evidence_metrics(case, {"hits": [{"chunk_id": "a", "context_parts": []}]})["recall"] == 1


def test_adaptive_keeps_same_attribute_for_different_products_and_business_versions():
    repo, chunks, _, _, service, request = fixture(count=3, texts=["Weight: 500 g."] * 3)
    chunks[1].evidence_structure.applicability.subjects = [ApplicableSubject(display_name="Other Product")]
    previous_document, previous_version = chunks[2].document_id, chunks[2].version_id
    chunks[2].document_id, chunks[2].version_id = uuid4(), uuid4()
    repo.documents[chunks[2].document_id] = repo.documents[previous_document].model_copy(update={"id": chunks[2].document_id})
    repo.versions[chunks[2].version_id] = repo.versions[previous_version].model_copy(update={
        "id": chunks[2].version_id, "document_id": chunks[2].document_id})
    chunks[2].metadata["business_version"] = "2"
    request.query, request.selection_mode, request.query_facets = "weight", "adaptive", ["weight"]
    result = service.search_evidence(request)
    assert len(result.hits) == 3


def test_twenty_hit_utf8_budget_includes_rank_digits_and_escaped_json():
    texts = ['Voltage "12 V" \\ circuit. 储存。' + str(i) for i in range(20)]
    _, _, _, _, service, request = fixture(count=20, texts=texts)
    request.top_k = 20
    request.max_evidence_bytes = 48000
    result = service.search_evidence(request)
    assert len(result.hits) == 20
    exact = len(encode_v2_hits(result.hits))
    request.max_evidence_bytes = exact - 1
    limited = service.search_evidence(request)
    assert len(limited.hits) == 19
    assert len(encode_v2_hits(limited.hits)) <= request.max_evidence_bytes
