from __future__ import annotations

import re
from collections import Counter
from time import perf_counter
from typing import Callable, Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from cuekb.services.retrieval import QueryRouter, rrf

from .corpus import FixtureSearch, ReplaySearch, Snapshot, tokens
from .extraction import claim_key, condition_match, extract_row, mentions
from .models import Artifact, Claim, Hit, Path, QueryCase, Result, digest

VARIANTS = ("H", "H+A", "H+B", "H+C", "H-wide")
STOP_WORDS = {"a", "an", "the", "is", "are", "can", "with", "to", "and", "of", "does", "what", "which"}


def validate_artifact(artifact: Artifact, snapshot: Snapshot) -> None:
    if artifact.policy != snapshot.manifest.policy:
        raise ValueError("artifact_policy_mismatch")
    ids = [link.id for link in artifact.links] + [claim.id for claim in artifact.claims]
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate_association_id")
    degrees = Counter(link.source for link in artifact.links)
    if any(degree > artifact.policy.neighbors for degree in degrees.values()):
        raise ValueError("artifact_neighbor_limit_exceeded")
    if len(artifact.links) > 24000 or len(artifact.claims) > 60000:
        raise ValueError("artifact_capacity_exceeded")


def _claim_proofs(claim: Claim, snapshot: Snapshot, case: QueryCase):
    identity = claim_key(claim.kb_id, claim.subject, claim.predicate, claim.object,
                         claim.polarity, claim.conditions, claim.unknown_conditions)
    if claim.id != uuid5(NAMESPACE_URL, identity):
        return []
    if (claim.state != "source_verified" or claim.unknown_conditions
            or condition_match(claim.conditions, case.context) != "match"):
        return []
    at = case.request.relations.at or snapshot.manifest.as_of
    if (claim.valid_from and at < claim.valid_from) or (claim.valid_until and at >= claim.valid_until):
        return []
    if any(entity not in snapshot.catalog or snapshot.catalog[entity].kb_id != claim.kb_id
           for entity in (claim.subject, claim.object)):
        return []
    for support_set in claim.support_sets:
        if not all(snapshot.proof_valid(proof, case) for proof in support_set):
            continue
        # A loaded artifact cannot turn arbitrary quoted text into a verified rule.
        if not all(any(candidate.id == claim.id and proof in candidate.support_sets[0]
                       for candidate in extract_row(snapshot.rows[proof.chunk_id], snapshot.manifest.entities))
                   for proof in support_set):
            continue
        return support_set
    return []


def run_query(
    snapshot: Snapshot,
    search: FixtureSearch | ReplaySearch,
    artifact: Artifact | None,
    case: QueryCase,
    variant: str,
    *,
    mode: Literal["off", "auto", "on"] = "on",
    dynamic: bool = False,
    fresh_snapshot: Callable[[], Snapshot] | None = None,
    clock: Callable[[], float] = perf_counter,
) -> Result:
    """Offline candidate experiment; the gold labels are never read here."""
    if variant not in VARIANTS or mode not in {"off", "auto", "on"}:
        raise ValueError("unsupported_experiment_mode")
    snapshot.require_scope(case)
    policy = snapshot.manifest.policy
    started = clock()
    request = case.request
    path, _ = QueryRouter(snapshot.manifest.exact_identifier_pattern).route(request)
    keyword_limit = 62 if variant == "H-wide" else policy.keyword_candidates
    vector_limit = 62 if variant == "H-wide" else policy.vector_candidates
    keyword = search.keyword(request.query, request.kb_ids, request.filters, keyword_limit)
    calls = 1
    vector = []
    reasons = []
    if path != "exact":
        if case.vector is not None:
            vector = search.vector(case.vector, request.kb_ids, request.filters, vector_limit)
            calls += 1
        else:
            reasons.append("query_vector_missing")
    rankings = [list(dict.fromkeys(chunk_id for chunk_id, _ in keyword))]
    if vector:
        rankings.append(list(dict.fromkeys(chunk_id for chunk_id, _ in vector)))
    base_scores = rrf(rankings)
    base_scores = {chunk_id: score for chunk_id, score in base_scores.items()
                   if snapshot.visible(chunk_id, request.kb_ids, request.filters)}
    baseline = sorted(base_scores, key=lambda chunk_id: (-base_scores[chunk_id], str(chunk_id)))
    keyword_ids = {hit[0] for hit in keyword}
    vector_ids = {hit[0] for hit in vector}
    baseline_finished = clock()
    association_started = baseline_finished
    deadline = association_started + policy.association_budget_ms / 1000
    paths: dict[UUID, list[Path]] = {}
    navigation: list[UUID] = []
    structured: list[UUID] = []
    groups = []
    expanded: set[UUID] = set()
    proofs_seen: set[UUID] = set()
    target_documents: Counter = Counter()
    edge_count = dynamic_calls = 0
    seed_ids = []
    seed_documents = Counter()
    for chunk_id in baseline:
        document = snapshot.rows[chunk_id].chunk.document_id
        if seed_documents[document] >= 2:
            continue
        seed_ids.append(chunk_id)
        seed_documents[document] += 1
        if len(seed_ids) == policy.seeds:
            break
    local_entities = [entity for entity in snapshot.manifest.entities if entity.kb_id in request.kb_ids]
    explicit_entities = {entity for _, _, entity in mentions(request.query, local_entities)}
    seed_entities = {entity for chunk_id in seed_ids
                     for _, _, entity in mentions(snapshot.rows[chunk_id].chunk.source_text, local_entities)}
    relevant_words = set(tokens(request.query)) - STOP_WORDS

    def relevance(chunk_id):
        return len(relevant_words & set(tokens(snapshot.rows[chunk_id].chunk.search_text)))

    def add(chunk_id, candidate_path, ranking):
        if clock() >= deadline:
            reasons.append("association_budget_exhausted")
            return False
        if chunk_id not in base_scores and chunk_id not in expanded:
            document = snapshot.rows[chunk_id].chunk.document_id
            if len(expanded) >= policy.added_candidates or target_documents[document] >= 3:
                reasons.append("candidate_budget_exhausted")
                return False
        new_proofs = set(candidate_path.proof_chunks) | {candidate_path.source, candidate_path.target}
        if len(proofs_seen | new_proofs) > policy.proof_chunks:
            reasons.append("proof_budget_exhausted")
            return False
        proofs_seen.update(new_proofs)
        if chunk_id not in ranking:
            ranking.append(chunk_id)
        if chunk_id not in base_scores and chunk_id not in expanded:
            expanded.add(chunk_id)
            target_documents[snapshot.rows[chunk_id].chunk.document_id] += 1
        paths.setdefault(chunk_id, []).append(candidate_path)
        return True

    enabled = variant in {"H+A", "H+B", "H+C"} and mode != "off" and path != "exact"
    if mode == "auto" and not (len(explicit_entities) >= 2 or re.search(r"\b(?:compatible|requires?|depend|compare|limitations?)\b", request.query, re.I)):
        enabled = False
        reasons.append("gate_not_triggered")
    if not enabled:
        reasons.append("exact_path" if path == "exact" else "association_off")
    elif not seed_ids:
        reasons.append("no_legal_seeds")
    else:
        if artifact is None:
            raise ValueError("association_artifact_required")
        validate_artifact(artifact, snapshot)
        catalog_match = artifact.catalog_sha256 == digest(snapshot.manifest.entities)
        binding_match = artifact.binding == snapshot.manifest.binding
        if not catalog_match:
            reasons.append("association_catalog_mismatch")
        if not binding_match:
            reasons.append("association_binding_mismatch")
        if variant in {"H+A", "H+C"} and catalog_match:
            links = sorted((link for link in artifact.links if link.source in seed_ids),
                           key=lambda link: (-relevance(link.target) if link.target in snapshot.rows else 0,
                                             -link.score, str(link.id)))
            for link in links:
                if clock() >= deadline:
                    reasons.append("association_budget_exhausted")
                    break
                if edge_count >= policy.preselected_edges:
                    reasons.append("edge_budget_exhausted")
                    break
                edge_count += 1
                if (link.target not in snapshot.rows or link.source not in snapshot.rows
                        or link.kb_id not in request.kb_ids
                        or link.kb_id != snapshot.rows[link.source].chunk.kb_id
                        or link.kb_id != snapshot.rows[link.target].chunk.kb_id
                        or not snapshot.visible(link.target, request.kb_ids, request.filters)
                        or not snapshot.proof_valid(link.proof, case)
                        or snapshot.rows[link.source].fingerprint != link.source_fingerprint
                        or snapshot.rows[link.target].fingerprint != link.target_fingerprint
                        or (link.kind == "semantic_neighbor" and not binding_match)
                        or link.kind == "near_duplicate" or not relevance(link.target)):
                    continue
                add(link.target, Path(source=link.source, target=link.target, source_type="navigation",
                    association_id=link.id, proof_chunks=[link.proof.chunk_id]), navigation)
        if variant in {"H+B", "H+C"} and catalog_match:
            for claim in sorted(artifact.claims, key=lambda item: (
                    not bool({item.subject, item.object} & explicit_entities), str(item.id))):
                if clock() >= deadline:
                    reasons.append("association_budget_exhausted")
                    break
                direction = request.relations.direction
                matches = ((direction in {"outgoing", "both"} and claim.subject in seed_entities | explicit_entities)
                    or (direction in {"incoming", "both"} and claim.object in seed_entities | explicit_entities))
                if claim.kb_id not in request.kb_ids or not matches:
                    continue
                if request.relations.types and claim.predicate not in request.relations.types:
                    continue
                if edge_count >= policy.preselected_edges:
                    reasons.append("edge_budget_exhausted")
                    break
                edge_count += 1
                support = _claim_proofs(claim, snapshot, case)
                if not support:
                    continue
                proof_ids = [proof.chunk_id for proof in support]
                if any(not relevance(chunk_id) for chunk_id in proof_ids):
                    continue
                # Admit the entire AND proof package or nothing.
                new_ids = set(proof_ids) - set(base_scores) - expanded
                source_id = next((chunk_id for chunk_id in seed_ids if
                    {claim.subject, claim.object} & {entity for _, _, entity in mentions(snapshot.rows[chunk_id].chunk.source_text, local_entities)}), None)
                if source_id is None or len(expanded | new_ids) > policy.added_candidates:
                    continue
                if len(proofs_seen | set(proof_ids) | {source_id}) > policy.proof_chunks:
                    reasons.append("proof_budget_exhausted")
                    continue
                package_counts = Counter(snapshot.rows[chunk_id].chunk.document_id for chunk_id in new_ids)
                if any(target_documents[document] + count > 3 for document, count in package_counts.items()):
                    continue
                for chunk_id in proof_ids:
                    add(chunk_id, Path(source=source_id, target=chunk_id, source_type="claim",
                        association_id=claim.id, proof_chunks=proof_ids), structured)
                groups.append({"claim_id": str(claim.id), "predicate": claim.predicate,
                               "polarity": claim.polarity, "proof_ids": proof_ids})
        if dynamic and catalog_match:
            # Only original query + original seeds. Expanded targets never become seeds.
            cue_names = {snapshot.catalog[entity].name for entity in seed_entities}
            for cue in sorted(cue_names):
                if dynamic_calls >= policy.dynamic_calls or clock() >= deadline:
                    break
                dynamic_calls += 1
                hits = search.keyword(f"{request.query} {cue}", request.kb_ids, request.filters,
                                      policy.dynamic_candidates)
                calls += 1
                source_id = next((chunk_id for chunk_id in seed_ids if cue.casefold() in snapshot.rows[chunk_id].chunk.source_text.casefold()), None)
                if source_id is None:
                    continue
                for chunk_id, _ in hits:
                    if snapshot.visible(chunk_id, request.kb_ids, request.filters) and relevance(chunk_id):
                        add(chunk_id, Path(source=source_id, target=chunk_id, source_type="dynamic",
                            proof_chunks=[source_id]), navigation)
        if "association_budget_exhausted" not in reasons:
            reasons.append("one_hop_complete")
    association_finished = clock()

    # A fresh frozen-state provider allows adversarial revocation/version-switch tests.
    latest = fresh_snapshot() if fresh_snapshot else snapshot
    latest.require_scope(case)
    valid_paths = {}
    link_by_id = {link.id: link for link in artifact.links} if artifact else {}
    claim_by_id = {claim.id: claim for claim in artifact.claims} if artifact else {}
    catalog_stable = digest(latest.manifest.entities) == digest(snapshot.manifest.entities)
    for target, target_paths in paths.items():
        for candidate_path in target_paths:
            required = set(candidate_path.proof_chunks) | {candidate_path.source, candidate_path.target}
            if (not catalog_stable or any(not latest.visible(chunk_id, request.kb_ids, request.filters)
                    or latest.rows[chunk_id].fingerprint != snapshot.rows[chunk_id].fingerprint for chunk_id in required)):
                continue
            if candidate_path.source_type == "navigation":
                link = link_by_id[candidate_path.association_id]
                if not latest.proof_valid(link.proof, case) or (link.kind == "semantic_neighbor" and artifact.binding != latest.manifest.binding):
                    continue
            elif candidate_path.source_type == "claim":
                if not _claim_proofs(claim_by_id[candidate_path.association_id], latest, case):
                    continue
            elif latest.manifest.binding != snapshot.manifest.binding:
                continue
            valid_paths.setdefault(target, []).append(candidate_path)
    if sum(map(len, valid_paths.values())) != sum(map(len, paths.values())):
        reasons.append("association_transition")
    paths = valid_paths
    navigation = [chunk_id for chunk_id in navigation if any(p.source_type != "claim" for p in paths.get(chunk_id, []))]
    structured = [chunk_id for chunk_id in structured if any(p.source_type == "claim" for p in paths.get(chunk_id, []))]
    scores = {chunk_id: score for chunk_id, score in base_scores.items() if latest.visible(chunk_id, request.kb_ids, request.filters)}
    for weight, ranking in ((policy.navigation_weight, navigation), (policy.claim_weight, structured)):
        for rank, chunk_id in enumerate(ranking, 1):
            scores[chunk_id] = scores.get(chunk_id, 0.0) + weight / (60 + rank)
    ordered = sorted(scores, key=lambda chunk_id: (-scores[chunk_id], str(chunk_id)))
    # Protect at least half the top-k direct evidence, without filling empty slots.
    protected = [chunk_id for chunk_id in baseline if chunk_id in scores][:min(4, request.top_k // 2)]
    selected = ordered[:request.top_k]
    for chunk_id in protected:
        if chunk_id not in selected:
            replacement = next((index for index in range(len(selected) - 1, -1, -1) if selected[index] not in protected), None)
            if replacement is not None:
                selected[replacement] = chunk_id
    selected.sort(key=lambda chunk_id: (-scores[chunk_id], str(chunk_id)))
    hits = []
    for chunk_id in selected:
        chunk = latest.rows[chunk_id].chunk
        sources = [name for name, ids in (("keyword", keyword_ids), ("vector", vector_ids),
                    ("navigation", set(navigation)), ("claim", set(structured))) if chunk_id in ids]
        hits.append(Hit(chunk_id=chunk_id, document_id=chunk.document_id, version_id=chunk.version_id,
            source_text=chunk.source_text, source_anchor=chunk.anchor.model_dump(mode="json"), score=scores[chunk_id],
            retrieval_sources=sources, paths=paths.get(chunk_id, [])))
    rendered_groups = []
    for group in groups:
        delivered = [chunk_id for chunk_id in group["proof_ids"] if chunk_id in selected
                     and any(p.association_id == UUID(group["claim_id"]) for p in paths.get(chunk_id, []))]
        if delivered:
            rendered_groups.append({"claim_id": group["claim_id"], "predicate": group["predicate"],
                "polarity": group["polarity"], "chunk_ids": [str(chunk_id) for chunk_id in delivered],
                "coverage": "covered" if len(delivered) == len(group["proof_ids"]) else "partial"})
    finished = clock()
    return Result(query_id=case.id, variant=variant, hits=hits, stop_reasons=list(dict.fromkeys(reasons)),
        counters={"initial_candidates": len(base_scores), "added_candidates": len(expanded),
                  "delivered_association_hits": sum(bool(hit.paths) for hit in hits), "preselected_edges": edge_count,
                  "proof_chunks": len(proofs_seen), "dynamic_calls": dynamic_calls, "search_calls": calls, "model_calls": 0},
        timings_ms={"replay_cpu_total": (finished - started) * 1000,
                    "baseline_cpu": (baseline_finished - started) * 1000,
                    "association_cpu": (association_finished - association_started) * 1000},
        snapshot_sha256=latest.sha256, artifact_id=artifact.id if enabled and artifact else None,
        evidence_groups=rendered_groups)
