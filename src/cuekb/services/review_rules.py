"""Review presentation and publish eligibility shared by UI/API/Worker."""
from collections import Counter
import re
from uuid import UUID

from cuekb.domain.models import Chunk
from cuekb.services.fact_rules import RULE_REVISION, catalog, matches
from cuekb.services.structure import REFERENCE, structural_role
from cuekb.services.evidence import scope_compatible


def blockers(chunks):
    by_id = {c.id: c for c in chunks}
    blocked = set()
    for chunk in chunks:
        s = chunk.evidence_structure
        if s.review_state == "excluded":
            continue
        if (s.review_state not in {"rule_confirmed", "human_confirmed"}
                or s.applicability.status == "ambiguous" or s.dependency_unresolved
                or (s.applicability.status == "unknown" and s.fact_kind != "general")):
            blocked.add(chunk.id)
        for link in s.context_links:
            target = by_id.get(link.chunk_id)
            if link.required and (target is None or target.evidence_structure.review_state == "excluded"):
                blocked.add(chunk.id)
            elif link.required and not scope_compatible(chunk, target, required=True, role=link.role):
                blocked.add(chunk.id)
    return blocked


def chunk_candidates(chunk, entities):
    preferred = matches(chunk.source_text, entities)
    if not preferred:
        for title in reversed(chunk.title_path):
            preferred = matches(title, entities)
            if preferred:
                break
    return [dict(entity_id=e["entity_id"], display_name=e["display_name"],
        product_model=e.get("product_model"), software_version=e.get("software_version"),
        kind=e.get("kind", "product")) for e, _ in preferred[:5] if e.get("entity_id")]


def issue_reason(chunk):
    s = chunk.evidence_structure
    return ("dependency_unresolved" if s.dependency_unresolved else s.review_reason or
        ("scope_conflict" if s.applicability.status == "ambiguous" else "scope_not_identified"))


def present(row, entities, targets):
    chunk = Chunk.model_validate(row["current"])
    s = chunk.evidence_structure
    dependencies = [link.chunk_id for link in s.context_links if link.required]
    candidates = []
    if s.dependency_unresolved or s.applicability.status == "resolved":
        refs = [m["quoted"] or m["plain"] for m in REFERENCE.finditer(chunk.source_text)]
        candidates = [c.id for c in targets if c.id != chunk.id and c.title_path and
            (any(label.strip().casefold() in c.title_path[-1].casefold() for label in refs)
             or structural_role(c.title_path[-1]))][:5]
    # Suggestions never mutate the source; user can choose these exact ranges.
    ranges = [(m.start(), m.end()) for m in re.finditer(r"[^;；\n]+(?:[;；]|$)", chunk.source_text)]
    return dict(id=row["id"], source_chunk_id=row["source_chunk_id"], source_text=chunk.source_text,
        title_path=chunk.title_path, anchor=chunk.anchor, applicability=s.applicability,
        fact_kind=s.fact_kind, review_state=s.review_state,
        reason=issue_reason(chunk),
        candidates=chunk_candidates(chunk, entities), dependencies=dependencies,
        dependency_candidates=candidates, dependency_issues=s.dependency_issues,
        dependency_choices=[dict(id=c.id, source_text=c.source_text[:500], title_path=c.title_path,
            role=structural_role(c.title_path[-1]) or "condition") for c in targets if c.id in candidates],
        split_suggestions=ranges[:50] if 1 < len(ranges) <= 50 else [])


def summarize(plan, chunks):
    counts = dict(Counter(c.evidence_structure.review_state for c in chunks))
    counts["confirmed_general"] = sum(c.evidence_structure.fact_kind == "general" and
        c.evidence_structure.review_state in {"rule_confirmed", "human_confirmed"} for c in chunks)
    blocked = blockers(chunks)
    included = any(c.evidence_structure.review_state != "excluded" for c in chunks)
    sections = {str(c.section_id): " / ".join(c.title_path) or "正文" for c in chunks if c.section_id}
    return dict(id=plan["id"], document_id=plan["document_id"], version_id=plan["version_id"],
        kb_id=plan["kb_id"], revision=plan["revision"], status=plan["status"], total=len(chunks),
        counts=counts, blocking=len(blocked), publish_eligible=included and not blocked,
        rule_revision=plan["rule_revision"], applied_job_id=plan.get("applied_job_id"),
        reason_counts=dict(Counter(issue_reason(c) for c in chunks if c.id in blocked)),
        sections=[dict(id=id, title=title) for id, title in list(sections.items())[:1000]],
        sections_truncated=len(sections)>1000)
