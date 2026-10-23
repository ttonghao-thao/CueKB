"""Offline experiment invariants, not real-model or production acceptance."""

from copy import deepcopy
from pathlib import Path
from uuid import uuid4

import pytest

from cuekb.experiments.associations.__main__ import main
from cuekb.experiments.associations.corpus import (
    FixtureSearch, Recording, ReplaySearch, Snapshot, load_dataset, write_json,
)
from cuekb.experiments.associations.discovery import build
from cuekb.experiments.associations.evaluation import (
    association_scores, paired_bootstrap, score, wilson,
)
from cuekb.experiments.associations.extraction import condition_match, extract_row
from cuekb.experiments.associations.models import Gold, GoldPlan, PathLabel, RelationLabel, digest
from cuekb.experiments.associations.query import run_query
from cuekb.schemas import SearchFilters

EXAMPLE = Path(__file__).resolve().parents[1] / "experiments/associations/example"


@pytest.fixture
def experiment():
    snapshot, cases, search = load_dataset(EXAMPLE)
    return snapshot, {case.id: case for case in cases}, search, build(snapshot, search)


def revised(snapshot, **changes):
    manifest = snapshot.manifest.model_copy(deep=True, update=changes)
    return Snapshot(manifest, list(snapshot.rows.values()))


def test_build_is_deterministic_and_does_not_include_draft_or_cross_kb_edges(experiment):
    snapshot, _, search, artifact = experiment
    rebuilt = build(snapshot, search)
    assert artifact.id == rebuilt.id
    assert artifact.links == rebuilt.links and artifact.claims == rebuilt.claims
    for link in artifact.links:
        assert snapshot.visible(link.source, [link.kb_id])
        assert snapshot.visible(link.target, [link.kb_id])
        assert snapshot.proof_valid(link.proof)
    assert artifact.counters["model_calls"] == 0
    for claim in artifact.claims:
        assert all(snapshot.proof_valid(proof) for support in claim.support_sets for proof in support)


def test_longest_name_and_negative_polarity_are_preserved(experiment):
    snapshot, _, _, artifact = experiment
    entities = {entity.name: entity.id for entity in snapshot.manifest.entities}
    pro = [claim for claim in artifact.claims if claim.subject == entities["Bulb A1 Pro"]]
    negative = next(claim for claim in pro if claim.object == entities["Zigbee"])
    assert negative.polarity == "negative" and negative.state == "source_verified"
    assert all(claim.subject != entities["Bulb A1"] for claim in pro)
    for link in artifact.links:
        left, right = snapshot.rows[link.source].chunk, snapshot.rows[link.target].chunk
        assert not (left.metadata.get("product_family") == right.metadata.get("product_family") == "Bulb"
                    and left.metadata.get("product_model") != right.metadata.get("product_model"))


def test_unparsed_restriction_never_becomes_unconditional_fact(experiment):
    snapshot, _, _, _ = experiment
    row = next(row for row in snapshot.rows.values() if row.chunk.metadata.get("product_model") == "Bulb A1"
               and snapshot.visible(row.chunk.id, [row.chunk.kb_id]))
    changed = row.model_copy(deep=True)
    changed.chunk.source_text = "Bulb A1 supports Zigbee unless it is in restricted mode."
    extracted = extract_row(changed, snapshot.manifest.entities)
    assert len(extracted) == 1
    assert extracted[0].state == "unresolved" and extracted[0].unknown_conditions


def test_document_software_scope_is_not_lost_during_extraction(experiment):
    snapshot, _, _, _ = experiment
    row = next(row for row in snapshot.rows.values() if row.chunk.metadata.get("product_model") == "Hub G")
    scoped = row.model_copy(deep=True)
    scoped.chunk.metadata["software_version"] = "2.1"
    scoped.chunk.source_text = "Hub G supports Zigbee."
    claim = extract_row(scoped, snapshot.manifest.entities)[0]
    assert condition_match(claim.conditions, {}) == "unknown"
    assert condition_match(claim.conditions, {claim.subject: {"firmware": "2.0"}}) == "mismatch"
    assert condition_match(claim.conditions, {claim.subject: {"firmware": "2.1"}}) == "match"


def test_reference_cue_does_not_label_every_shared_entity_a_reference(experiment):
    snapshot, _, _, artifact = experiment
    bulb = next(row.chunk.id for row in snapshot.rows.values() if row.chunk.metadata.get("product_model") == "Bulb A1"
                and snapshot.visible(row.chunk.id, [row.chunk.kb_id]))
    protocol = next(row.chunk.id for row in snapshot.rows.values() if row.chunk.title_path == ["zigbee"])
    link = next(link for link in artifact.links if link.source == bulb and link.target == protocol)
    assert link.kind == "semantic_neighbor"


@pytest.mark.parametrize("firmware,expected", [(None, "unknown"), ("2.0", "mismatch"), ("2.1", "match"),
                                               ("2.1.0", "match"), ("2.10", "match"), ("2.1-beta", "unknown")])
def test_firmware_is_role_bound_and_not_lexicographically_compared(experiment, firmware, expected):
    snapshot, _, _, artifact = experiment
    claim = next(claim for claim in artifact.claims if claim.predicate == "compatible_with")
    gateway = next(entity.id for entity in snapshot.manifest.entities if entity.name == "Hub G")
    context = {gateway: {"firmware": firmware}} if firmware else {}
    assert claim.conditions[0].entity_id == gateway
    assert condition_match(claim.conditions, context) == expected


def test_unknown_or_mismatched_conditions_do_not_activate_claim(experiment):
    snapshot, cases, search, artifact = experiment
    case = cases["condition"]
    claim = next(claim for claim in artifact.claims if claim.predicate == "compatible_with")
    for context in ({}, case.context):
        result = run_query(snapshot, search, artifact, case.model_copy(update={"context": context}), "H+B")
        assert not any(path.association_id == claim.id for hit in result.hits for path in hit.paths)
    gateway = claim.conditions[0].entity_id
    result = run_query(snapshot, search, artifact, case.model_copy(update={"context": {gateway: {"firmware": "2.1"}}}), "H+B")
    assert any(path.association_id == claim.id for hit in result.hits for path in hit.paths)


def test_off_and_exact_do_not_read_association_artifacts(experiment):
    snapshot, cases, search, _ = experiment
    baseline = run_query(snapshot, search, None, cases["multi"], "H")
    disabled = run_query(snapshot, search, None, cases["multi"], "H+C", mode="off", dynamic=True)
    assert disabled.hits == baseline.hits
    assert disabled.counters["dynamic_calls"] == 0
    assert disabled.artifact_id is None
    exact = run_query(snapshot, search, None, cases["exact"], "H+C", dynamic=True)
    assert exact.counters["search_calls"] == 1
    assert exact.counters["model_calls"] == 0 and not any(hit.paths for hit in exact.hits)


def test_offline_query_does_not_construct_production_settings(experiment, monkeypatch):
    snapshot, cases, search, _ = experiment
    monkeypatch.setenv("CUEKB_BACKEND", "production")
    monkeypatch.setenv("CUEKB_API_KEY_PEPPER", "")
    monkeypatch.setenv("CUEKB_MODEL_CONFIG_KEY", "")
    result = run_query(snapshot, search, None, cases["exact"], "H")
    assert result.counters["search_calls"] == 1


def test_query_never_uses_gold_labels_for_retrieval(experiment):
    snapshot, cases, search, artifact = experiment
    case = cases["multi"]
    changed = case.model_copy(update={"gold": Gold(answerable=False)})
    first = run_query(snapshot, search, artifact, case, "H+C", dynamic=True)
    second = run_query(snapshot, search, artifact, changed, "H+C", dynamic=True)
    assert first.hits == second.hits and first.evidence_groups == second.evidence_groups


def test_hard_filters_and_authorization_are_never_relaxed(experiment):
    snapshot, cases, search, artifact = experiment
    result = run_query(snapshot, search, artifact, cases["negative"], "H+C", dynamic=True)
    assert result.hits
    assert all(snapshot.rows[hit.chunk_id].chunk.metadata["product_model"] == "Bulb A1 Pro" for hit in result.hits)
    denied = cases["multi"].model_copy(update={"principal_id": uuid4()})
    with pytest.raises(PermissionError):
        run_query(snapshot, search, artifact, denied, "H+C")
    disabled = revised(snapshot, disabled_principals=[cases["multi"].principal_id])
    with pytest.raises(PermissionError):
        run_query(snapshot, search, artifact, cases["multi"], "H+C", fresh_snapshot=lambda: disabled)


def test_deleting_source_removes_paths_even_if_target_remains_legal(experiment):
    snapshot, cases, search, artifact = experiment
    first = run_query(snapshot, search, artifact, cases["multi"], "H+C")
    assert any(hit.paths for hit in first.hits)
    source = next(path.source for hit in first.hits for path in hit.paths)
    document = snapshot.rows[source].chunk.document_id
    latest = revised(snapshot, deleted_documents=[document])
    result = run_query(snapshot, search, artifact, cases["multi"], "H+C", fresh_snapshot=lambda: latest)
    assert all(hit.document_id != document for hit in result.hits)
    assert all(source not in path.proof_chunks and path.source != source for hit in result.hits for path in hit.paths)
    assert "association_transition" in result.stop_reasons


def test_changed_text_with_same_chunk_id_does_not_keep_old_proof(experiment):
    snapshot, cases, search, artifact = experiment
    result = run_query(snapshot, search, artifact, cases["multi"], "H+C")
    source = next(path.source for hit in result.hits for path in hit.paths)
    rows = deepcopy(list(snapshot.rows.values()))
    changed = next(row for row in rows if row.chunk.id == source)
    changed.chunk.source_text = "Revised text with no previous assertion."
    latest = Snapshot(snapshot.manifest, rows)
    current = run_query(snapshot, search, artifact, cases["multi"], "H+C", fresh_snapshot=lambda: latest)
    assert all(path.source != source for hit in current.hits for path in hit.paths)
    assert all(hit.source_text != snapshot.rows[source].chunk.source_text for hit in current.hits if hit.chunk_id == source)


def test_semantic_links_do_not_survive_model_binding_change(experiment):
    snapshot, cases, search, artifact = experiment
    binding = snapshot.manifest.binding.model_copy(update={"revision": "other-model-revision"})
    latest = revised(snapshot, binding=binding)
    result = run_query(snapshot, search, artifact, cases["multi"], "H+A", fresh_snapshot=lambda: latest)
    semantic_ids = {link.id for link in artifact.links if link.kind == "semantic_neighbor"}
    assert all(path.association_id not in semantic_ids for hit in result.hits for path in hit.paths)


def test_corrupted_claim_cannot_relabel_valid_quote(experiment):
    snapshot, cases, search, artifact = experiment
    corrupted = artifact.model_copy(deep=True)
    gateway = next(entity.id for entity in snapshot.manifest.entities if entity.name == "Hub G")
    target_claim = next(claim for claim in corrupted.claims if claim.predicate == "supports_capability")
    target_claim.subject = gateway if target_claim.subject != gateway else uuid4()
    result = run_query(snapshot, search, corrupted, cases["multi"], "H+B")
    assert all(path.association_id != target_claim.id for hit in result.hits for path in hit.paths)


def test_dynamic_discovery_is_one_round_and_shares_global_limits(experiment):
    snapshot, cases, _, artifact = experiment
    base = FixtureSearch(snapshot)

    class Search:
        keywords = []

        def keyword(self, query, kb_ids, filters, limit):
            self.keywords.append(query)
            return base.keyword(query, kb_ids, filters, limit)

        def vector(self, vector, kb_ids, filters, limit):
            return base.vector(vector, kb_ids, filters, limit)

    search = Search()
    result = run_query(snapshot, search, artifact, cases["multi"], "H+C", dynamic=True)
    assert result.counters["dynamic_calls"] <= 2 and len(search.keywords) <= 3
    assert result.counters["added_candidates"] <= 24
    assert result.counters["preselected_edges"] <= 32 and result.counters["proof_chunks"] <= 96
    assert all(query.startswith(cases["multi"].request.query) for query in search.keywords)


def test_expansion_stops_before_new_calls_when_budget_is_exhausted(experiment):
    snapshot, cases, search, artifact = experiment
    times = iter([0, 0.01, 0.5])
    clock = lambda: next(times, 0.5)
    result = run_query(snapshot, search, artifact, cases["multi"], "H+C", dynamic=True, clock=clock)
    assert result.counters["dynamic_calls"] == 0
    assert not any(hit.paths for hit in result.hits)
    assert "association_budget_exhausted" in result.stop_reasons


def test_duplicate_evidence_and_alternative_plans_are_scored_correctly(experiment):
    snapshot, cases, search, _ = experiment
    case = cases["multi"]
    result = run_query(snapshot, search, None, case, "H-wide")
    bulb = next(hit for hit in result.hits if hit.chunk_id in case.gold.units["bulb"])
    gateway = next(hit for hit in result.hits if hit.chunk_id in case.gold.units["gateway"])
    duplicate = result.model_copy(update={"hits": [bulb, bulb]})
    partial = score(case, duplicate, 2)
    assert partial["precision"] == 0.5 and partial["recall"] == 0.5 and not partial["complete"]
    full = score(case, result.model_copy(update={"hits": [bulb, gateway]}), 2)
    assert full["precision"] == full["recall"] == 1 and full["complete"]
    alternative = case.model_copy(deep=True)
    alternative.gold.plans.append(GoldPlan(units=["gateway"], document_ids=[gateway.document_id]))
    assert score(alternative, result.model_copy(update={"hits": [gateway]}), 2)["complete"]


def test_bootstrap_keeps_query_pairs_and_marks_small_family_samples(experiment):
    snapshot, cases, search, _ = experiment
    case = cases["multi"]
    row = score(case, run_query(snapshot, search, None, case, "H"), 8)
    comparison = paired_bootstrap([row], [row], "recall", iterations=30)
    assert comparison["difference"] == 0 and comparison["ci95"] == [0, 0]
    assert comparison["status"] == "insufficient_families"
    assert wilson(0, 0) is None
    assert wilson(9, 10)[0] < 0.9 < wilson(9, 10)[1]


def test_independent_relation_and_path_labels_are_required(experiment):
    snapshot, cases, search, artifact = experiment
    result = run_query(snapshot, search, artifact, cases["multi"], "H+C")
    empty = association_scores(artifact, [result], [], [])
    assert empty["association_accuracy"]["measured"] is None
    path = next(path for hit in result.hits for path in hit.paths)
    claim = artifact.claims[0]
    labelled = association_scores(artifact, [result],
        [RelationLabel(claim_id=claim.id, correct=False, family="manual-label", split="test")],
        [PathLabel(query_id=result.query_id, path_sha256=digest(path), correct=False)])
    assert labelled["association_accuracy"]["measured"] == 0
    assert labelled["wrong_association_rate"]["delivered_paths"]["H+C"]["measured"] == 1
    assert labelled["association_accuracy"]["unlabelled"] > 0


def test_replay_has_no_fixture_fallback_and_requires_capture_limit(experiment):
    snapshot, cases, _, _ = experiment
    case = cases["multi"]
    replay = ReplaySearch([Recording(method="keyword", query=case.request.query, kb_ids=case.request.kb_ids,
        filters=case.request.filters, hits=[], captured_limit=20)])
    assert replay.keyword(case.request.query, case.request.kb_ids, case.request.filters, 20) == []
    with pytest.raises(ValueError, match="missing_or_insufficient_recording"):
        replay.keyword(case.request.query, case.request.kb_ids, case.request.filters, 21)
    with pytest.raises(ValueError, match="missing_or_insufficient_recording"):
        replay.vector(case.vector, case.request.kb_ids, SearchFilters(), 20)


def test_bad_vectors_and_ambiguous_aliases_fail_before_build(experiment):
    snapshot, _, _, _ = experiment
    rows = deepcopy(list(snapshot.rows.values()))
    rows[0].vector = [1.0]
    with pytest.raises(ValueError, match="corpus_vector_dimension_mismatch"):
        Snapshot(snapshot.manifest, rows)
    manifest = snapshot.manifest.model_dump(mode="json")
    manifest["entities"][1]["aliases"] = [manifest["entities"][0]["name"]]
    with pytest.raises(ValueError, match="ambiguous_entity_alias"):
        type(snapshot.manifest).model_validate(manifest)


def test_unknown_nested_filter_cannot_be_silently_ignored(experiment):
    _, cases, _, _ = experiment
    raw = cases["multi"].model_dump(mode="json")
    raw["request"]["filters"]["unsupported_product_scope"] = "another model"
    with pytest.raises(ValueError, match="unknown_filter_field"):
        type(cases["multi"]).model_validate(raw)


def test_cli_end_to_end_and_atomic_output(tmp_path, capsys):
    artifact = tmp_path / "build.json"
    report = tmp_path / "report.json"
    assert main(["validate", str(EXAMPLE)]) == 0
    assert main(["build", str(EXAMPLE), str(artifact)]) == 0
    assert main(["run", str(EXAMPLE), str(artifact), str(report), "--dynamic"]) == 0
    import json

    content = json.loads(report.read_text())
    assert len(content["results"]) == 25
    assert content["run_manifest"]["baseline_kind"] == "local_bm25_cosine_fixture"
    assert content["production_gates"]["status"] == "not_evaluated"
    assert all(group["http_latency_ms"]["measured"] is None for group in content["quality"].values())
    assert main(["build", str(EXAMPLE), str(artifact)]) == 2
    assert "output_already_exists" in capsys.readouterr().err
    write_json(tmp_path / "atom.json", {"valid": 1})
    with pytest.raises(ValueError, match="output_already_exists"):
        write_json(tmp_path / "atom.json", {"valid": 2})
    assert json.loads((tmp_path / "atom.json").read_text()) == {"valid": 1}
