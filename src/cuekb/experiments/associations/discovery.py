from __future__ import annotations

from time import perf_counter
import re
from uuid import NAMESPACE_URL, uuid5

from cuekb.schemas import SearchFilters

from .corpus import FixtureSearch, ReplaySearch, Snapshot, cosine, tokens
from .extraction import extract, make_proof, mentions
from .models import Artifact, Link, digest


def build(snapshot: Snapshot, search: FixtureSearch | ReplaySearch) -> Artifact:
    """Bounded neighbors: replay ANN candidates, or exact cosine for fixtures only."""
    started = perf_counter()
    policy = snapshot.manifest.policy
    links = []
    calls = 0
    for _, row in sorted(snapshot.rows.items(), key=lambda item: str(item[0])):
        source = row.chunk
        if not snapshot.visible(source.id, [source.kb_id]):
            continue
        candidates = search.keyword(source.search_text, [source.kb_id], SearchFilters(), policy.build_candidates)
        calls += 1
        if row.vector is not None:
            candidates += search.vector(row.vector, [source.kb_id], SearchFilters(), policy.build_candidates)
            calls += 1
        source_tokens = set(tokens(source.source_text))
        local_entities = [e for e in snapshot.manifest.entities if e.kb_id == source.kb_id]
        reference_entities = {entity for start, _, entity in mentions(source.source_text, local_entities)
            if re.search(r"(?:\bsee|\brefer to|\bas described in)\s+$", source.source_text[:start], re.I)}
        choices = []
        for target_id in dict.fromkeys(candidate[0] for candidate in candidates):
            if not snapshot.visible(target_id, [source.kb_id]):
                continue
            target_row = snapshot.rows[target_id]
            target = target_row.chunk
            if target.document_id == source.document_id:
                continue
            # Do not connect similar variants merely because names and vectors overlap.
            if (source.metadata.get("product_family") and source.metadata.get("product_family") == target.metadata.get("product_family")
                    and source.metadata.get("product_model") != target.metadata.get("product_model")):
                continue
            target_tokens = set(tokens(target.source_text))
            overlap = len(source_tokens & target_tokens) / max(1, len(source_tokens | target_tokens))
            similarity = cosine(row.vector, target_row.vector) if row.vector is not None and target_row.vector is not None else 0.0
            target_entities = {entity for _, _, entity in mentions(target.source_text, local_entities)}
            reference = bool(reference_entities & target_entities)
            duplicate = digest(source.source_text) == digest(target.source_text)
            if not reference and not duplicate and (row.vector is None or target_row.vector is None or similarity < policy.semantic_floor):
                continue
            kind = "explicit_reference" if reference else "near_duplicate" if duplicate else "semantic_neighbor"
            # This is a navigation score, never a factual probability.
            score = 0.5 * max(0, similarity) + 0.3 * overlap + 0.2 * reference
            choices.append(Link(id=uuid5(NAMESPACE_URL, digest([source.id, target.id, kind])),
                source=source.id, target=target.id, kb_id=source.kb_id,
                source_fingerprint=row.fingerprint, target_fingerprint=target_row.fingerprint,
                kind=kind, score=score, features={"cosine": similarity, "token_jaccard": overlap, "explicit_reference": float(reference)},
                proof=make_proof(row)))
        documents: dict = {}
        semantic = 0
        selected = 0
        for link in sorted(choices, key=lambda item: (-item.score, str(item.id))):
            document = snapshot.rows[link.target].chunk.document_id
            if documents.get(document, 0) >= 2 or (link.kind == "semantic_neighbor" and semantic >= policy.semantic_neighbors):
                continue
            links.append(link)
            selected += 1
            semantic += link.kind == "semantic_neighbor"
            documents[document] = documents.get(document, 0) + 1
            if selected >= policy.neighbors:
                break
    claims, truncated = extract(snapshot)
    identity = digest({"manifest": snapshot.manifest, "corpus": snapshot.corpus_sha256, "links": links, "claims": claims})
    return Artifact(id=uuid5(NAMESPACE_URL, identity), manifest_sha256=digest(snapshot.manifest),
        corpus_sha256=snapshot.corpus_sha256, catalog_sha256=digest(snapshot.manifest.entities),
        binding=snapshot.manifest.binding, policy=policy, links=links, claims=claims,
        build_ms=(perf_counter() - started) * 1000,
        counters={"chunks": len(snapshot.rows), "links": len(links), "claims": len(claims),
                  "verified_claims": sum(claim.state == "source_verified" for claim in claims),
                  "support_sets_truncated": truncated, "search_calls": calls, "model_calls": 0})
