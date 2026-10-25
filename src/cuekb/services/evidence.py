"""Conservative, deterministic evidence scope and reference rules.

These functions never inspect a query. A document tag and a textual mention are
not evidence that a fact applies to a subject.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any

from cuekb.domain.models import (
    Applicability,
    ApplicableCondition,
    ApplicableSubject,
    Chunk,
    ContextLink,
    ContextProvenance,
    EvidenceStructure,
    PROCESSING_REVISION,
    SourceSpan,
)

def _identity(subject: ApplicableSubject) -> tuple[str, str | None, str | None]:
    identity = str(subject.entity_id) if subject.entity_id else subject.display_name.casefold()
    return identity, subject.product_model, subject.software_version


def _subject_keys(scope: Applicability) -> frozenset[tuple[str, str | None, str | None]]:
    return frozenset(_identity(subject) for subject in scope.subjects)


def scope_matches(
    chunk: Chunk, product_model: str | None = None, software_version: str | None = None
) -> bool:
    """V2 exact local applicability filter; document metadata is not a fallback."""
    if product_model is None and software_version is None:
        return True
    scope = chunk.evidence_structure.applicability
    if scope.status != "resolved":
        return False
    # Both requested values must belong to the same explicitly declared subject.
    return any(
        (product_model is None or subject.product_model == product_model)
        and (software_version is None or subject.software_version == software_version)
        for subject in scope.subjects
    )


def scope_compatible(
    hit: Chunk, candidate: Chunk, *, required: bool = False, role: str | None = None
) -> bool:
    """Gate before proximity/unit selection, including for explicit dependencies."""
    if (hit.kb_id, hit.document_id, hit.version_id) != (
        candidate.kb_id, candidate.document_id, candidate.version_id
    ):
        return False
    if candidate.id == hit.id:
        return True
    left = hit.evidence_structure.applicability
    right = candidate.evidence_structure.applicability
    if left.status != "resolved" or right.status != "resolved":
        # Product identity and an audited structural dependency are independent.
        # Never relax optional proximity, conflicting/ambiguous declarations, or
        # links without frozen source/target provenance.
        if (not required or left.status == "ambiguous" or right.status == "ambiguous"
                or (left.status == "unknown" and left.subjects)
                or (right.status == "unknown" and right.subjects)):
            return False
        if left.conditions and right.conditions and {c.text for c in left.conditions} != {c.text for c in right.conditions}:
            return False
        return any(
            link.required and link.chunk_id == candidate.id and link.role == role
            and trusted_dependency(hit, candidate, link)
            for link in hit.evidence_structure.context_links
        )
    left_keys, right_keys = _subject_keys(left), _subject_keys(right)
    shared_structure = required and role in {"attribution", "table_header", "condition", "exception"}
    if left_keys != right_keys and not (shared_structure and left_keys <= right_keys):
        return False
    left_conditions = {c.text for c in left.conditions}
    right_conditions = {c.text for c in right.conditions}
    if left_conditions != right_conditions:
        # Explicitly declared structural dependencies may supply conditions that
        # are absent on the fact, but never combine two distinct known conditions.
        if not shared_structure or (left_conditions and right_conditions):
            return False
    return True


def trusted_dependency(hit: Chunk, target: Chunk, link: ContextLink) -> bool:
    proof = link.provenance
    if (proof is None or not hit.evidence_structure.source_unit_id
            or not target.evidence_structure.source_unit_id
            or proof.source_unit_id != hit.evidence_structure.source_unit_id
            or proof.target_unit_id != target.evidence_structure.source_unit_id):
        return False
    if proof.kind == "curated":
        return proof.reference_text is None or proof.reference_text in hit.source_text
    if proof.kind == "parser_reference":
        return bool(proof.reference_text and proof.reference_text in hit.source_text
                    and link.role in {"attribution", "condition", "exception", "table_header"})
    return (link.role == "continuation"
            and proof.source_unit_id == proof.target_unit_id)


def validate_evidence_links(chunks: Sequence[Chunk]) -> None:
    """Validate the complete version after every target (including forward refs) exists."""
    by_id = {chunk.id: chunk for chunk in chunks}
    if len(by_id) != len(chunks):
        raise ValueError("duplicate_chunk_id")
    adjacency: dict[Any, list[Any]] = {}
    for chunk in chunks:
        seen = set()
        adjacency[chunk.id] = []
        for link in chunk.evidence_structure.context_links:
            if link.chunk_id == chunk.id:
                raise ValueError("context_self_link")
            if link.chunk_id in seen:
                raise ValueError("duplicate_context_link")
            seen.add(link.chunk_id)
            target = by_id.get(link.chunk_id)
            if target is None:
                raise ValueError("context_target_missing")
            if (chunk.kb_id, chunk.document_id, chunk.version_id) != (
                target.kb_id, target.document_id, target.version_id
            ):
                raise ValueError("context_scope_mismatch")
            if link.provenance and not trusted_dependency(chunk, target, link):
                raise ValueError("invalid_context_provenance")
            adjacency[chunk.id].append(link.chunk_id)
    # Iterative DFS keeps validation safe even for large imported documents.
    done, visiting = set(), set()
    for start in adjacency:
        if start in done:
            continue
        stack = [(start, False)]
        while stack:
            node, exiting = stack.pop()
            if exiting:
                visiting.discard(node)
                done.add(node)
                continue
            if node in visiting:
                raise ValueError("context_reference_cycle")
            if node in done:
                continue
            visiting.add(node)
            stack.append((node, True))
            stack.extend((target, False) for target in adjacency[node] if target not in done)


def bind_block_dependencies(chunks: Sequence[Chunk]) -> None:
    """Turn parser continuation units into bounded forward references.

    A split unit forms a directed star from each continuation to its first
    fragment. The head is marked unresolved when a whole unit cannot be
    represented in one hop without cycles; no fragment claims completeness.
    """
    units: dict[str, list[Chunk]] = {}
    for chunk in chunks:
        unit = chunk.evidence_structure.source_unit_id
        if unit:
            units.setdefault(unit, []).append(chunk)
    for group in units.values():
        continued = [c for c in group if c.evidence_structure.dependency_unresolved]
        if len(continued) < 2:
            continue
        head = min(continued, key=lambda c: c.ordinal)
        for chunk in continued:
            if chunk.id != head.id:
                chunk.evidence_structure.context_links = [
                    ContextLink(chunk_id=head.id, role="continuation", required=True,
                        provenance=ContextProvenance(kind="parser_continuation",
                            source_unit_id=chunk.evidence_structure.source_unit_id,
                            target_unit_id=head.evidence_structure.source_unit_id,
                            provenance_id="parser:continuation"))
                ]
            # A partial cell/paragraph is not complete just because its first
            # fragment could be attached. All pieces retain this explicit flag.
            chunk.evidence_structure.dependency_unresolved = True
    validate_evidence_links(chunks)


def resolve_applicability(
    title_path: Sequence[str], source_spans: Sequence[SourceSpan], source_unit_id: str,
    snapshot: Mapping[str, Any] | None, source_unit_aliases: Sequence[str] = (),
) -> Applicability:
    """Use only frozen bindings, exact reviewed heading aliases and explicit defaults."""
    if not snapshot:
        reasons = ["missing_binding"]
        if not title_path and not any(s.label == "table_row_label" for s in source_spans):
            reasons.append("structure_missing")
        return Applicability(reason_codes=reasons)
    declarations: list[Applicability] = []
    for binding in snapshot.get("scope_bindings", []):
        if not isinstance(binding, Mapping):
            raise ValueError("invalid_scope_binding")
        path = binding.get("title_path")
        unit = binding.get("source_unit_id")
        if path is None and unit is None:
            raise ValueError("scope_binding_requires_selector")
        if path is not None and (not path or list(title_path[:len(path)]) != path):
            continue
        if unit is not None and unit not in {source_unit_id, *source_unit_aliases}:
            continue
        declarations.append(_declaration(binding, source_kind="curated"))

    aliases: list[ApplicableSubject] = []
    for entity in snapshot.get("approved_entities", []):
        if not isinstance(entity, Mapping):
            raise ValueError("invalid_approved_entity")
        name = entity.get("display_name", entity.get("name"))
        if not name:
            raise ValueError("approved_entity_missing_display_name")
        names = {str(name).casefold(), *(str(a).casefold() for a in entity.get("aliases", []))}
        labels = [*title_path, *(span.text.strip() for span in source_spans if span.label == "table_row_label")]
        matched = [heading for heading in labels if heading.casefold() in names]
        if matched:
            spans = [span for span in source_spans
                     if span.label in {"heading", "table_row_label"} and span.text.strip() in matched]
            aliases.append(ApplicableSubject(
                entity_id=entity.get("entity_id", entity.get("id")), display_name=name,
                product_model=entity.get("product_model"),
                software_version=entity.get("software_version"), source_kind="reviewed_alias",
                provenance_id=entity.get("provenance_id"), source_spans=spans[:20],
            ))
    if aliases:
        # Distinct nested product headings are intentionally ambiguous. We do
        # not guess that the last named product silently overrides its parent.
        unique = {_identity(subject): subject for subject in aliases}
        declarations.append(Applicability(
            status="resolved" if len(unique) == 1 else "ambiguous",
            subjects=list(unique.values())[:8],
        ))
    default = snapshot.get("document_default")
    if default:
        declarations.append(_declaration(default, source_kind="curated"))
    if not declarations:
        reasons = ["no_reviewed_match" if (snapshot.get("scope_bindings") or
            snapshot.get("approved_entities")) else "missing_binding"]
        if not title_path and not any(s.label == "table_row_label" for s in source_spans):
            reasons.append("structure_missing")
        return Applicability(reason_codes=reasons)
    # An absent model/version is incomplete information about an entity, not a
    # second entity. Compare entity sets first, then merge compatible attributes.
    def entity_key(subject):
        return str(subject.entity_id) if subject.entity_id else subject.display_name.casefold()

    keys = {frozenset(entity_key(s) for s in scope.subjects) for scope in declarations}
    grouped: dict[str, list[list[ApplicableSubject]]] = {}
    for declaration in declarations:
        declared: dict[str, list[ApplicableSubject]] = {}
        for subject in declaration.subjects:
            declared.setdefault(entity_key(subject), []).append(subject)
        for identity, subjects_in_declaration in declared.items():
            grouped.setdefault(identity, []).append(subjects_in_declaration)
    subjects = {}
    conflict = False
    for group in grouped.values():
        variants = group[0]
        for declaration_variants in group[1:]:
            pairs = [(left, right) for left in variants for right in declaration_variants
                     if all(getattr(left, field) is None or getattr(right, field) is None
                            or getattr(left, field) == getattr(right, field)
                            for field in ("product_model", "software_version"))]
            # Multiple versions explicitly allowed in one declaration remain
            # alternatives. Every declared alternative must have a compatible
            # counterpart; never silently narrow a conflicting declaration.
            if (any(not any(left is value for left, _ in pairs) for value in variants)
                    or any(not any(right is value for _, right in pairs) for value in declaration_variants)):
                conflict = True
                variants = [s for declaration_group in group for s in declaration_group]
                break
            merged = {}
            for left, right in pairs:
                preferred = left if left.source_kind == "curated" else right
                spans = {span.model_dump_json(): span for s in (left, right) for span in s.source_spans}
                subject = preferred.model_copy(update={
                    "product_model": left.product_model or right.product_model,
                    "software_version": left.software_version or right.software_version,
                    "source_spans": list(spans.values())[:20],
                })
                merged[_identity(subject)] = subject
            variants = list(merged.values())
        subjects.update({_identity(s): s for s in variants})
    conditions = {c.text: c for scope in declarations for c in scope.conditions}
    ambiguous = (conflict or len(keys) > 1 or any(d.status != "resolved" for d in declarations)
                 or len(subjects) > 8 or len(conditions) > 20)
    return Applicability(
        status="ambiguous" if ambiguous else "resolved",
        subjects=list(subjects.values())[:8], conditions=list(conditions.values())[:20],
        reason_codes=["scope_conflict"] if ambiguous else [],
    )


def _declaration(value: Mapping[str, Any], *, source_kind: str) -> Applicability:
    subjects = []
    for subject in value.get("subjects", []):
        item = dict(subject)
        item.setdefault("source_kind", source_kind)
        subjects.append(ApplicableSubject.model_validate(item))
    conditions = []
    for condition in value.get("conditions", []):
        item = {"text": condition} if isinstance(condition, str) else dict(condition)
        item.setdefault("source_kind", source_kind)
        conditions.append(ApplicableCondition.model_validate(item))
    return Applicability(status="resolved", subjects=subjects, conditions=conditions)


def structure_for(
    *, block_kind: str, unit_id: str, title_path: Sequence[str], source_spans: Sequence[SourceSpan],
    snapshot: Mapping[str, Any] | None, unresolved: bool = False,
    source_unit_aliases: Sequence[str] = (),
    parser_unit_key: str | None = None,
) -> EvidenceStructure:
    applicability = resolve_applicability(title_path, source_spans, unit_id, snapshot,
                                        source_unit_aliases=source_unit_aliases)
    entity_ids = {str(s.entity_id) for s in applicability.subjects if s.entity_id}
    aliases = sorted({str(alias) for entity in (snapshot or {}).get("approved_entities", [])
                      if str(entity.get("entity_id", entity.get("id"))) in entity_ids
                      for alias in entity.get("aliases", [])})
    return EvidenceStructure(
        block_kind=block_kind, source_unit_id=unit_id,
        origin_source_unit_id=next(iter(source_unit_aliases), None),
        parser_unit_key=parser_unit_key,
        applicability=applicability,
        retrieval_aliases=aliases if applicability.status == "resolved" else [],
        structure_revision=PROCESSING_REVISION, dependency_unresolved=unresolved,
    )


def source_unit_id(identity: str, version_id: Any = None) -> str:
    canonical = json.dumps([str(version_id or "unbound"), identity], ensure_ascii=False)
    return "unit:" + hashlib.sha256(canonical.encode()).hexdigest()


def build_search_text(
    chunk: Chunk, document_name: str, *, template: str = "evidence-v2", max_chars: int = 16000,
) -> str:
    """Shared immutable model-input template; never writes back to source_text.

    Only resolved scope can augment the model input. The generation/model
    template is independently versioned by the registered model configuration.
    """
    if max_chars < 1:
        raise ValueError("invalid_model_input_budget")
    if template != "evidence-v2":
        raise ValueError("unsupported_model_input_template")
    context = [document_name, " / ".join(chunk.title_path)]
    scope = chunk.evidence_structure.applicability
    if scope.status == "resolved":
        context.extend(subject.display_name for subject in scope.subjects)
        context.extend(condition.text for condition in scope.conditions)
    # Augmentation is bounded separately so a large administrator declaration
    # does not displace the whole primary fact from the search/model input.
    prefix = "\n".join(part for part in context if part)[:min(2000, max_chars // 4)]
    return ((prefix + "\n") if prefix else "") + chunk.source_text[:max_chars - (len(prefix) + 1 if prefix else 0)]
