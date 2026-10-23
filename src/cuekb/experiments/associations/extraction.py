"""Conservative, anchored English rules. No gold labels or model calls."""

from __future__ import annotations

import re
from uuid import NAMESPACE_URL, UUID, uuid5

from .corpus import Snapshot
from .models import Claim, Condition, CorpusRow, Entity, Proof, digest


VERBS = {
    "requires": ("depends_on", "positive"),
    "depends on": ("depends_on", "positive"),
    "supports": ("supports_capability", "positive"),
    "does not support": ("supports_capability", "negative"),
    "doesn't support": ("supports_capability", "negative"),
    "is compatible with": ("compatible_with", "positive"),
    "is not compatible with": ("compatible_with", "negative"),
}


def mentions(text: str, entities: list[Entity]) -> list[tuple[int, int, UUID]]:
    candidates = []
    for entity in entities:
        for name in [entity.name, *entity.aliases]:
            for match in re.finditer(r"(?<![\w-])" + re.escape(name) + r"(?![\w-])", text, re.I):
                candidates.append((match.start(), match.end(), entity.id))
    # Longest entity wins an overlapping name: A1 Pro must not become A1.
    chosen = []
    for candidate in sorted(candidates, key=lambda item: (-(item[1] - item[0]), item[0], str(item[2]))):
        if not any(candidate[0] < end and candidate[1] > start for start, end, _ in chosen):
            chosen.append(candidate)
    return sorted(chosen, key=lambda item: (item[0], item[1], str(item[2])))


def make_proof(row: CorpusRow, start: int = 0, end: int | None = None) -> Proof:
    end = len(row.chunk.source_text) if end is None else end
    return Proof(chunk_id=row.chunk.id, version_id=row.chunk.version_id, fingerprint=row.fingerprint,
                 start=start, end=end, quote_sha256=digest(row.chunk.source_text[start:end]))


def claim_key(kb_id, subject, predicate, object_id, polarity, conditions, unknown):
    return digest([kb_id, subject, predicate, object_id, polarity, conditions, unknown])


def extract_row(row: CorpusRow, entities: list[Entity]) -> list[Claim]:
    text = row.chunk.source_text
    local = [entity for entity in entities if entity.kb_id == row.chunk.kb_id]
    claims = []
    verified_count = 0
    by_id = {entity.id: entity for entity in local}
    # Do not split version numbers or decimal values at their internal dots.
    boundaries = list(re.finditer(r"(?<!\d)[.!?](?!\d)|[\n;]", text))
    start = 0
    for boundary_end in [match.end() for match in boundaries] + [len(text)]:
        raw = text[start:boundary_end]
        offset = start + len(raw) - len(raw.lstrip())
        sentence = raw.strip().rstrip(".!?")
        start = boundary_end
        found = mentions(sentence, local)
        if len(found) < 2 or found[0][0] != 0:
            continue
        left, right = found[:2]
        verb = " ".join(sentence[left[1]:right[0]].casefold().split())
        if verb not in VERBS or left[2] == right[2]:
            continue
        predicate, polarity = VERBS[verb]
        tail = sentence[right[1]:].strip()
        conditions = []
        unknown = []
        if tail:
            matched = False
            for entity in local:
                for name in [entity.name, *entity.aliases]:
                    pattern = r"(?:only )?with " + re.escape(name) + r" firmware (\d+(?:\.\d+)*)( or later)?"
                    match = re.fullmatch(pattern, tail, re.I)
                    if match and entity.id in {left[2], right[2]}:
                        conditions.append(Condition(entity_id=entity.id, field="firmware",
                            op="gte" if match[2] else "eq", value=match[1],
                            scheme="vendor_numeric_v1" if match[2] else "exact"))
                        matched = True
                        break
                if matched:
                    break
            if not matched:
                # Every unexplained suffix is a restriction, not an unconditional fact.
                unknown.append(tail[:500])
        if predicate == "supports_capability" and by_id[right[2]].kind not in {"protocol", "capability", "function", "feature"}:
            unknown.append("predicate_object_type_unsupported")
        for metadata_field, condition_field in (("software_version", "firmware"), ("region", "region")):
            value = row.chunk.metadata.get(metadata_field)
            if value is None:
                continue
            owner = next((entity_id for entity_id in (left[2], right[2])
                          if row.chunk.metadata.get("product_model") in [by_id[entity_id].name, *by_id[entity_id].aliases]), None)
            if owner is None or not isinstance(value, str) or not value:
                unknown.append(f"unresolved_document_{metadata_field}_scope")
            else:
                condition = Condition(entity_id=owner, field=condition_field, value=value)
                if condition not in conditions:
                    conditions.append(condition)
        if not unknown and verified_count >= 10:
            unknown.append("per_chunk_claim_limit")
        verified_count += not unknown
        key = claim_key(row.chunk.kb_id, left[2], predicate, right[2], polarity, conditions, unknown)
        claims.append(Claim(id=uuid5(NAMESPACE_URL, key), kb_id=row.chunk.kb_id,
            subject=left[2], object=right[2], predicate=predicate, polarity=polarity,
            conditions=conditions, unknown_conditions=unknown,
            state="unresolved" if unknown else "source_verified",
            support_sets=[[make_proof(row, offset, offset + len(sentence))]]))
        if len(claims) >= 20:
            break
    return claims


def extract(snapshot: Snapshot) -> tuple[list[Claim], int]:
    by_id: dict[UUID, Claim] = {}
    truncated = 0
    for _, row in sorted(snapshot.rows.items(), key=lambda item: str(item[0])):
        if not snapshot.visible(row.chunk.id, [row.chunk.kb_id]):
            continue
        for claim in extract_row(row, snapshot.manifest.entities):
            previous = by_id.get(claim.id)
            if previous is None:
                by_id[claim.id] = claim
            elif claim.support_sets[0] not in previous.support_sets:
                if len(previous.support_sets) < 3:
                    previous.support_sets.append(claim.support_sets[0])
                else:
                    truncated += 1
    return sorted(by_id.values(), key=lambda claim: str(claim.id)), truncated


def condition_match(conditions: list[Condition], context: dict[UUID, dict[str, str]]) -> str:
    unknown = False
    for condition in conditions:
        value = context.get(condition.entity_id, {}).get(condition.field)
        if value is None:
            unknown = True
            continue
        if condition.op == "eq":
            if value != condition.value:
                return "mismatch"
        elif condition.scheme == "vendor_numeric_v1":
            if not re.fullmatch(r"\d+(?:\.\d+)*", value):
                unknown = True
                continue
            left = [int(part) for part in value.split(".")]
            right = [int(part) for part in condition.value.split(".")]
            width = max(len(left), len(right))
            if left + [0] * (width - len(left)) < right + [0] * (width - len(right)):
                return "mismatch"
        else:
            unknown = True
    return "unknown" if unknown else "match"
