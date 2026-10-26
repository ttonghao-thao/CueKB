"""CPU-only, source-backed structure and scope rules (never query-derived)."""
from __future__ import annotations

from dataclasses import replace
import re

from cuekb.domain.models import Applicability, ApplicableSubject, SourceSpan
from cuekb.services.evidence import source_unit_id, structure_for

RULE_REVISION = "scope-rules-1"
MAX_FACTS = 10000
PROPERTY = re.compile(r"\b(?:voltage|weight|price|shelf life|storage|capacity|power|dimensions?|rated|ingredients?|allergens?|delivery)\b|电压|重量|价格|保质期|功率|容量|尺寸|配送|成分|过敏", re.I)
BUNDLE = re.compile(r"\b(?:bundle|kit|mixed.{0,15}(?:box|pack)|combination.{0,15}(?:box|pack))\b|套件|组合装|混合礼盒", re.I)
COMPARE = re.compile(r"\b(?:versus|compared|unlike|whereas|than)\b|相比|比较|对比", re.I)
GENERAL = re.compile(r"^(?:note\b|disclaimer\b|glossary\b|brand (?:history|introduction)\b|company (?:overview|profile)\b|备注|免责声明|术语|品牌介绍|公司简介)", re.I)
LIST = re.compile(r"(?m)^\s*(?:[·•*+\-]\s+|\d+[.)]\s+)")


def catalog(snapshot):
    # Non-product entities (e.g. glossary terms) are never product ownership.
    return [e for e in (snapshot or {}).get("approved_entities", [])
            if e.get("kind", "product") in {"product", "bundle", "model"}]


def matches(text, entities):
    result = []
    for entity in entities:
        names = {entity["display_name"], *entity.get("aliases", [])}
        if entity.get("product_model"):
            names.add(entity["product_model"])
        hits = []
        for name in names:
            # ASCII alphanumeric boundaries stop A100 matching A1000. Chinese
            # text has no whitespace word boundary, so exact phrases are used.
            pattern = (r"(?<![\w-])" if name[0].isascii() and name[0].isalnum() else "") + re.escape(name)
            pattern += r"(?![\w-])" if name[-1].isascii() and name[-1].isalnum() else ""
            hits.extend((m.start(), m.end()) for m in re.finditer(pattern, text, re.I))
        if hits:
            result.append((entity, sorted(set(hits))))
    return result


def subject(entity, spans=()):
    return ApplicableSubject(entity_id=entity.get("entity_id"), display_name=entity["display_name"],
        product_model=entity.get("product_model"), software_version=entity.get("software_version"),
        source_kind="reviewed_alias", provenance_id=RULE_REVISION, source_spans=list(spans)[:20])


def classify(block, snapshot):
    """Prefer local explicit ownership; a mere mention only produces a candidate."""
    structure = block.evidence_structure
    conditions = list(structure.applicability.conditions)
    entities = catalog(snapshot)
    local = matches(block.text, entities)
    template = (snapshot or {}).get("rule_template", {})
    has_property = lambda value: bool(PROPERTY.search(value) or any(
        term.casefold() in value.casefold() for term in template.get("attribute_terms", [])))
    bundle = BUNDLE.search(block.text) and (len(local) > 1 or
        re.search(r"RMB\s*\d|\d+\s*元|\bcontains?\b|包含", block.text, re.I))
    structure.fact_kind = ("bundle" if bundle else
        "comparison" if len(local) > 1 and COMPARE.search(block.text) else
        "specification" if has_property(block.text) else "statement")
    if structure.applicability.status == "resolved":
        structure.review_state = "rule_confirmed"
        structure.review_reason = "frozen_scope"
    # A subject preceding an explicit attribute can end an inherited section.
    explicit = [(e, spans) for e, spans in local if any(
        not block.text[:start].strip(" ·•*-+\n\t\"“'")
        and has_property(block.text[end:end + 60]) for start, end in spans)]
    if len(explicit) == 1 and len(local) == 1 and structure.fact_kind != "bundle":
        entity, _ = explicit[0]
        # Explicit curated mappings must not silently contradict local evidence.
        previous = structure.applicability
        if any(s.source_kind == "curated" and not (
               (s.entity_id and str(s.entity_id) == str(entity.get("entity_id")))
               or s.display_name.casefold() == entity["display_name"].casefold()) for s in previous.subjects):
            structure.applicability = Applicability(status="ambiguous", subjects=[*previous.subjects, subject(entity)][:8], reason_codes=["scope_conflict"])
        else:
            structure.applicability = Applicability(status="resolved", subjects=[subject(entity, block.source_spans)])
            structure.review_state, structure.review_reason = "rule_confirmed", "explicit_subject_attribute"
    # Any multiple-product body must be reviewed. Neither an inherited title nor
    # a dual-product mapping proves that every value applies to both products.
    if len(local) > 1:
        structure.applicability = Applicability(status="ambiguous", subjects=[subject(e) for e, _ in local][:8], reason_codes=["scope_conflict"])
        structure.review_state, structure.review_reason = "pending", "mixed_subjects"
    if len(structure.applicability.subjects) > 1 and structure.fact_kind == "specification":
        structure.review_state, structure.review_reason = "pending", "mixed_subjects"
    if structure.fact_kind == "bundle":
        bundles = [e for e, _ in local if e.get("kind") == "bundle"]
        if len(bundles) == 1:
            structure.applicability = Applicability(status="resolved", subjects=[subject(bundles[0])])
            structure.review_state, structure.review_reason = "rule_confirmed", "bundle_identity"
        else:
            structure.applicability = Applicability()
            structure.review_state, structure.review_reason = "pending", "bundle_identity_missing"
    if not local and structure.applicability.status == "unknown":
        label = (block.title_path[-1] if block.title_path else block.text.lstrip("* "))
        label = re.sub(r"^(?:[IVX]+|\d+(?:\.\d+)*|[A-Z])[.)]\s+", "", label)
        if GENERAL.match(label) or label.casefold() in {v.casefold() for v in template.get("general_headings", [])}:
            structure.fact_kind, structure.review_state, structure.review_reason = "general", "rule_confirmed", "explicit_general_section"
    if structure.applicability.status == "ambiguous":
        structure.review_state = "pending"
    if local and not explicit and structure.applicability.status == "resolved":
        owned = {str(s.entity_id) if s.entity_id else s.display_name.casefold() for s in structure.applicability.subjects}
        mentioned = {str(e.get("entity_id")) if e.get("entity_id") else e["display_name"].casefold() for e, _ in local}
        if not mentioned <= owned:
            structure.review_state, structure.review_reason = "pending", "scope_conflict"
    resolved = {str(s.entity_id) for s in structure.applicability.subjects if s.entity_id}
    structure.retrieval_aliases = sorted({a for e in entities if str(e.get("entity_id")) in resolved
        for a in e.get("aliases", [])})[:800] if structure.applicability.status == "resolved" else []
    structure.review_provenance = RULE_REVISION if structure.review_state == "rule_confirmed" else None
    structure.applicability.conditions = conditions
    return block


def refine_blocks(blocks, snapshot, version_id):
    """Split structural list items and independently stated product clauses."""
    result = []
    for block in blocks:
        if block.evidence_structure.block_kind == "table":
            result.extend(_table_columns(block, snapshot, version_id))
            continue
        if sum(bool(s.parser_item_id) and s.label != "heading" for s in block.source_spans) > 1:
            # Multiple page provenances cannot be assigned invented per-fact
            # character coordinates. Keep actual spans and require review.
            block.evidence_structure.dependency_unresolved = True
            result.append(classify(block, snapshot))
            continue
        if any(s.label == "code" for s in block.source_spans):
            result.append(block)
            continue
        marks = list(LIST.finditer(block.text))
        boundaries = [0]
        # Keep list introductions with their conditions: only split a list that
        # begins at the unit boundary. Conditional introductions remain intact.
        if len(marks) > 1 and not block.text[:marks[0].start()].strip():
            boundaries.extend(m.start() for m in marks[1:])
        elif len(matches(block.text, catalog(snapshot))) > 1:
            # Split at sentence/semicolon boundaries only when every clause has
            # an explicit named subject. Qualifiers remain with their sentence.
            trial = [0, *(m.end() for m in re.finditer(r"[;；]\s*|(?<=[.!?。！？])\s+", block.text)), len(block.text)]
            if all(matches(block.text[a:b], catalog(snapshot)) for a, b in zip(trial, trial[1:]) if block.text[a:b].strip()):
                boundaries = trial[:-1]
        boundaries = sorted(set([*boundaries, len(block.text)]))
        for index, (start, end) in enumerate(zip(boundaries, boundaries[1:])):
            piece = block.text[start:end]
            if not piece.strip():
                continue
            if len(boundaries) == 2:
                result.append(classify(block, snapshot))
                continue
            key = f"{block.evidence_structure.parser_unit_key}:piece:{block.fragment_offset}:{block.anchor.start_offset}:fact:{start}:{end}"
            anchor = block.anchor.model_copy(deep=True)
            text_source = any(s.label in {"text", "list"} and s.anchor.start_offset is not None for s in block.source_spans)
            if text_source and anchor.start_offset is not None:
                anchor.start_offset += block.fragment_offset + start
                anchor.end_offset = anchor.start_offset + len(piece)
            spans = [s for s in block.source_spans if s.label == "heading"]
            spans.append(SourceSpan(label="text", text=piece, anchor=anchor))
            unit = source_unit_id(key, version_id)
            origin = (snapshot or {}).get("origin_unit_aliases", {}).get(key)
            if not origin and (snapshot or {}).get("origin_version_id"):
                origin = source_unit_id(key, snapshot["origin_version_id"])
            aliases = (origin,) if origin else ()
            structure = structure_for(block_kind="text", unit_id=unit, title_path=block.title_path,
                source_spans=spans, snapshot=snapshot, parser_unit_key=key, source_unit_aliases=aliases)
            structure.parent_unit_id = block.evidence_structure.source_unit_id
            # Splitting a fragment cannot repair a partially recovered source.
            structure.dependency_unresolved = block.evidence_structure.dependency_unresolved
            result.append(classify(replace(block, text=piece, anchor=anchor, source_spans=spans,
                evidence_structure=structure, source_unit_aliases=aliases), snapshot))
        if len(result) > MAX_FACTS:
            raise ValueError("document_fact_limit_exceeded")
    return result


def _table_columns(block, snapshot, version_id):
    """A table with multiple product columns must not expose the other values."""
    headers = [s for s in block.source_spans if s.label == "table_header"]
    if not headers:
        return [classify(block, snapshot)]
    columns = [v.strip() for v in headers[0].text.splitlines()[0].strip().strip("|").split("|")]
    identities = [(i, matches(label, catalog(snapshot))) for i, label in enumerate(columns)]
    products = [(i, m[0][0]) for i, m in identities if len(m) == 1]
    if len(products) < 2:
        return [classify(block, snapshot)]
    identities = [str(e.get("entity_id", e["display_name"])) for _, e in products]
    if (len(products) != len(columns)-1 or any(i==0 for i,_ in products)
            or len(set(identities)) != len(identities)):
        # Unknown product/unit/condition columns must never disappear merely
        # because the catalog recognized the two other columns.
        block.evidence_structure.applicability = Applicability()
        block.evidence_structure.review_state = "pending"
        block.evidence_structure.review_reason = "table_alignment_missing"
        return [block]
    cells = {s.column_index: s for s in block.source_spans if s.label in {"table_cell", "table_row_label"}}
    if any(i not in cells for i, _ in products) or 0 not in cells:
        block.evidence_structure.review_state = "pending"
        block.evidence_structure.review_reason = "table_alignment_missing"
        block.evidence_structure.applicability = Applicability()
        return [block]
    result = []
    for i, entity in products:
        spans = [s for s in block.source_spans if s.label == "heading"]
        # Real source spans for row label/value; the selected header is proven
        # by the complete original header, not an invented cell coordinate.
        spans.extend([headers[0], cells[0], cells[i]])
        text = f"{columns[i]} | {cells[0].text.strip()} | {cells[i].text.strip()}"
        key = f"{block.evidence_structure.parser_unit_key}:column:{i}"
        structure = structure_for(block_kind="table", unit_id=source_unit_id(key, version_id),
            title_path=block.title_path, source_spans=spans, snapshot=snapshot, parser_unit_key=key)
        structure.parent_unit_id = block.evidence_structure.source_unit_id
        structure.applicability = Applicability(status="resolved", subjects=[subject(entity, spans)],
            conditions=structure.applicability.conditions)
        structure.fact_kind, structure.review_state = "specification", "rule_confirmed"
        structure.review_reason, structure.review_provenance = "product_column", RULE_REVISION
        structure.retrieval_aliases = entity.get("aliases", [])[:100]
        result.append(replace(block, text=text, source_spans=spans, evidence_structure=structure))
    return result
