"""Gold-aware scoring lives here, never in discovery or query expansion."""

from __future__ import annotations

import math
import random
from collections import defaultdict
from statistics import mean

from .models import Artifact, PathLabel, QueryCase, RelationLabel, Result, digest


def score(case: QueryCase, result: Result, k: int) -> dict:
    if not 1 <= k <= 20:
        raise ValueError("evaluation_k_out_of_range")
    gold = case.gold
    returned = result.hits[:k]
    chunk_ids = {hit.chunk_id for hit in returned}
    covered = {unit for unit, alternatives in gold.units.items() if chunk_ids & set(alternatives)}
    # Maximum bipartite matching gives at most one precision credit per position
    # and per equivalent unit, even with overlapping alternative chunk sets.
    assigned = {}

    def assign(position, seen):
        for unit, alternatives in gold.units.items():
            if unit in seen or returned[position].chunk_id not in alternatives:
                continue
            seen.add(unit)
            if unit not in assigned or assign(assigned[unit], seen):
                assigned[unit] = position
                return True
        return False

    for position in range(len(returned)):
        assign(position, set())
    relevant_docs = {hit.document_id for hit in returned
                     if any(hit.chunk_id in alternatives for alternatives in gold.units.values())}
    recalls = [len(covered & set(plan.units)) / len(plan.units) for plan in gold.plans]
    document_coverage = [len(relevant_docs & set(plan.document_ids)) / len(plan.document_ids) for plan in gold.plans]
    complete = bool(gold.plans) and any(set(plan.units) <= covered and set(plan.document_ids) <= relevant_docs
                                      for plan in gold.plans)
    return {
        "query_id": case.id, "family": case.family, "category": case.category,
        "answerable": gold.answerable, "k": k, "returned": len(returned),
        "precision": len(assigned) / k if gold.answerable else None,
        "precision_returned": len(assigned) / len(returned) if gold.answerable and returned else None,
        "recall": max(recalls, default=0.0) if gold.answerable else None,
        "document_coverage": max(document_coverage, default=0.0) if gold.answerable else None,
        "complete": complete if gold.answerable else None,
        "multi_document": any(len(plan.document_ids) > 1 for plan in gold.plans),
        "multi_evidence": any(len(plan.units) > 1 for plan in gold.plans),
        "forbidden_hits": len(chunk_ids & set(gold.forbidden_chunks)),
        "no_answer_misleading": bool(chunk_ids & set(gold.forbidden_chunks)) if not gold.answerable else None,
        "error": result.error_code,
    }


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def summarize(rows: list[dict]) -> dict:
    def aggregate(key, subset=rows):
        values = [float(row[key]) for row in subset if row[key] is not None]
        return {"mean": mean(values) if values else None, "n": len(values), "sum": sum(values)}

    multi = [row for row in rows if row["multi_document"]]
    multi_evidence = [row for row in rows if row["multi_evidence"]]
    return {"queries": len(rows), "errors": sum(row["error"] is not None for row in rows),
            "precision_at_k": aggregate("precision"), "recall_at_k": aggregate("recall"),
            "document_coverage": aggregate("document_coverage", multi),
            "multi_evidence_complete": aggregate("complete", multi_evidence),
            "no_answer_misleading": aggregate("no_answer_misleading"),
            "forbidden_hits": sum(row["forbidden_hits"] for row in rows)}


def paired_bootstrap(baseline: list[dict], comparison: list[dict], metric: str, *, seed: int = 17, iterations: int = 1000) -> dict:
    if not 1 <= iterations <= 5000:
        raise ValueError("bootstrap_iteration_limit")
    left = {row["query_id"]: row for row in baseline}
    right = {row["query_id"]: row for row in comparison}
    if set(left) != set(right):
        raise ValueError("unpaired_evaluation_rows")
    groups = defaultdict(list)
    for query_id in sorted(left):
        a, b = left[query_id], right[query_id]
        if a["family"] != b["family"]:
            raise ValueError("unpaired_query_family")
        if a[metric] is not None and b[metric] is not None:
            groups[a["family"]].append(float(b[metric]) - float(a[metric]))
    if not groups:
        return {"difference": None, "ci95": None, "n": 0, "families": 0, "status": "no_samples"}
    families = sorted(groups)
    deltas = [delta for group in groups.values() for delta in group]
    rng = random.Random(seed)
    draws = [mean([delta for family in rng.choices(families, k=len(families)) for delta in groups[family]])
             for _ in range(iterations)]
    return {"difference": mean(deltas), "ci95": [percentile(draws, 0.025), percentile(draws, 0.975)],
            "n": len(deltas), "families": len(families),
            "status": "insufficient_families" if len(families) < 5 else "estimated"}


def report(cases: list[QueryCase], results: list[Result], *, k: int = 8) -> dict:
    by_case = {case.id: case for case in cases}
    grouped = defaultdict(list)
    timing = defaultdict(list)
    for result in results:
        grouped[result.variant].append(score(by_case[result.query_id], result, k))
        timing[result.variant].append(result.timings_ms["replay_cpu_total"])
    summaries = {}
    for variant, rows in grouped.items():
        summaries[variant] = summarize(rows)
        summaries[variant]["categories"] = {
            category: summarize([row for row in rows if row["category"] == category])
            for category in sorted({row["category"] for row in rows})}
        summaries[variant]["replay_cpu_ms"] = {"p50": percentile(timing[variant], 0.5), "p95": percentile(timing[variant], 0.95)}
        summaries[variant]["http_latency_ms"] = {"measured": None, "status": "not_measured"}
    comparisons = {}
    for variant, rows in grouped.items():
        if variant == "H" or "H" not in grouped:
            continue
        multi_left = [row for row in grouped["H"] if row["multi_evidence"]]
        multi_right = [row for row in rows if row["multi_evidence"]]
        simple_left = [row for row in grouped["H"] if row["category"] == "simple"]
        simple_right = [row for row in rows if row["category"] == "simple"]
        comparisons[variant] = {
            "multi_complete": paired_bootstrap(multi_left, multi_right, "complete"),
            "simple_precision": paired_bootstrap(simple_left, simple_right, "precision")}
    return {"quality": summaries, "paired_comparisons": comparisons, "rows": dict(grouped),
            "association_accuracy": {"measured": None, "status": "independent_relation_labels_required"},
            "wrong_association_rate": {"measured": None, "status": "independent_path_labels_required"},
            "production_gates": {"status": "not_evaluated", "reason": "offline_quality_and_fixture_checks_are_not_production_acceptance"}}


def wilson(correct: int, total: int) -> list[float] | None:
    if not total:
        return None
    z = 1.959963984540054
    proportion = correct / total
    denominator = 1 + z * z / total
    center = (proportion + z * z / (2 * total)) / denominator
    radius = z * math.sqrt(proportion * (1 - proportion) / total + z * z / (4 * total * total)) / denominator
    return [max(0, center - radius), min(1, center + radius)]


def association_scores(artifact: Artifact, results: list[Result], relations: list[RelationLabel], path_labels: list[PathLabel]) -> dict:
    relation_map = {label.claim_id: label for label in relations}
    path_map = {(label.query_id, label.path_sha256): label for label in path_labels}
    if len(relation_map) != len(relations) or len(path_map) != len(path_labels):
        raise ValueError("duplicate_association_label")
    predicted = {claim.id for claim in artifact.claims if claim.state == "source_verified"}
    checked = predicted & set(relation_map)
    correct = sum(relation_map[claim_id].correct for claim_id in checked)
    expected = {claim_id for claim_id, label in relation_map.items() if label.correct}
    accuracy = {"measured": correct / len(checked) if checked else None, "correct": correct,
                "n": len(checked), "unlabelled": len(predicted - checked), "ci95": wilson(correct, len(checked)),
                "status": "fully_labelled" if checked and checked == predicted else "partial_or_missing_labels"}
    recall = {"measured": len(predicted & expected) / len(expected) if expected else None,
              "correct": len(predicted & expected), "expected": len(expected)}
    paths_by_variant = defaultdict(set)
    for result in results:
        for hit in result.hits:
            for path in hit.paths:
                paths_by_variant[result.variant].add((result.query_id, digest(path)))
    errors = {}
    for variant in {result.variant for result in results}:
        keys = paths_by_variant[variant]
        labelled = keys & set(path_map)
        wrong = sum(not path_map[key].correct for key in labelled)
        errors[variant] = {"measured": wrong / len(labelled) if labelled else None, "wrong": wrong,
                          "n": len(labelled), "unlabelled": len(keys - labelled), "ci95": wilson(wrong, len(labelled)),
                          "status": "fully_labelled" if labelled and labelled == keys else "partial_or_missing_labels"}
    return {"association_accuracy": accuracy, "association_recall": recall,
            "wrong_association_rate": {"delivered_paths": errors, "candidate_pool": {"measured": None, "status": "not_labelled"}}}
