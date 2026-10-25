"""Freeze bounded, auditable necessary section links; never infer a product."""
from collections import defaultdict
from itertools import islice
import re

from cuekb.domain.models import ContextLink, ContextProvenance
from cuekb.services.context import build_sections
from cuekb.services.evidence import scope_compatible, validate_evidence_links

# Only explicit references to structural conditions are automatic. Headings and
# proximity alone do not make every fact depend on an entire section.
REFERENCE = re.compile(
    r'(?:\bsee\s+(?:section\s+)?|\bsubject\s+to\s+(?:section\s+)?|参见|详见|见章节|见)'
    r'\s*[:：]?\s*(?:["“「《](?P<quoted>[^"”」》\n]{1,200})["”」》]|'
    r'(?P<plain>\d+(?:\.\d+)*(?=\s|[.;。；，,()（）]|$)|[^\n.;。；，,()（）]{1,200}))', re.I)


def structural_role(title):
    lower = title.casefold()
    if re.search(r'\bexceptions?\b', lower) or any(word in lower for word in ("例外", "除外")):
        return "exception"
    if re.search(r'\b(?:applicability|scope of application)\b', lower) or "适用范围" in lower:
        return "attribution"
    if re.search(r'\b(?:conditions?|limitations?)\b', lower) or any(word in lower for word in ("条件", "限制")):
        return "condition"
    return None


def bind_section_dependencies(chunks, snapshot, aliases=None):
    """Selectors are frozen upload/reprocess inputs, not query-time guesses."""
    aliases = aliases or {}
    build_sections(chunks)
    by_id = {chunk.id: chunk for chunk in chunks}
    by_path, by_label, by_unit = defaultdict(list), defaultdict(list), defaultdict(list)
    for chunk in chunks:
        by_path[tuple(chunk.title_path)].append(chunk)
        if chunk.title_path:
            label = chunk.title_path[-1].strip().casefold()
            by_label[label].append(chunk)
            number = re.match(r'^(?:第\s*)?(\d+(?:\.\d+)*)(?:[\s.、章]|$)', label)
            if number:
                by_label[number[1]].append(chunk)
        for unit in {chunk.evidence_structure.source_unit_id, *aliases.get(chunk.id, ())} - {None}:
            by_unit[unit].append(chunk)

    def issue(chunk, code):
        chunk.evidence_structure.dependency_unresolved = True
        if code not in chunk.evidence_structure.dependency_issues:
            chunk.evidence_structure.dependency_issues.append(code)

    def select(selector):
        selected = (by_unit.get(selector.get("source_unit_id"), [])
                    if selector.get("source_unit_id") else by_path.get(tuple(selector["title_path"]), []))
        if selector.get("title_path"):
            selected = [c for c in selected if c.title_path == selector["title_path"]]
        if not selected:
            raise ValueError("dependency_selector_not_found")
        if len({c.section_id for c in selected}) > 1:
            raise ValueError("ambiguous_dependency_selector")
        return selected

    def add(source, targets, role, kind, provenance_id, reference=None):
        for target in targets:
            if source.id == target.id:
                if kind == "curated":
                    raise ValueError("context_self_link")
                continue
            links = source.evidence_structure.context_links
            if any(link.chunk_id == target.id for link in links):
                continue
            if len(links) >= 20:
                issue(source, "dependency_limit_exceeded")
                break
            if kind == "parser_reference":
                # Reject cycles without recursing or performing unbounded graph work.
                pending, visited = [target.id], set()
                while pending and len(visited) < 1000:
                    node = pending.pop()
                    if node == source.id:
                        break
                    if node in visited:
                        continue
                    visited.add(node)
                    pending.extend(link.chunk_id for link in by_id[node].evidence_structure.context_links)
                if node == source.id or pending:
                    issue(source, "dependency_cycle" if node == source.id else "dependency_limit_exceeded")
                    continue
            proof = ContextProvenance(kind=kind, provenance_id=provenance_id,
                source_unit_id=source.evidence_structure.source_unit_id,
                target_unit_id=target.evidence_structure.source_unit_id, reference_text=reference)
            link = ContextLink(chunk_id=target.id, role=role, provenance=proof)
            links.append(link)
            if not scope_compatible(source, target, required=True, role=role):
                issue(source, "unsafe_dependency")

    reviewed_references = set()
    for binding in (snapshot or {}).get("dependency_bindings", []):
        sources, targets = select(binding["source"]), select(binding["target"])
        reference = binding.get("reference_text")
        if reference:
            sources = [source for source in sources if reference in source.source_text]
            if not sources:
                raise ValueError("dependency_reference_not_in_source")
            cited = list(REFERENCE.finditer(reference))
            if len(cited) > 1:
                raise ValueError("dependency_reference_must_identify_one_reference")
            reviewed_reference = cited[0][0] if cited else reference
        for source in sources:
            add(source, targets, binding["role"], "curated", binding["provenance_id"], reference)
            if reference:
                reviewed_references.add((source.id, reviewed_reference))
    validate_evidence_links(chunks)
    for source in chunks:
        references = list(islice(REFERENCE.finditer(source.source_text), 21))
        if len(references) > 20:
            issue(source, "dependency_limit_exceeded")
        for match in references[:20]:
            if len(match[0]) > 500:
                issue(source, "dependency_limit_exceeded")
                continue
            if (source.id, match[0]) in reviewed_references:
                continue
            label = (match["quoted"] or match["plain"]).strip()
            targets = by_label.get(label.casefold(), [])
            role = structural_role(targets[0].title_path[-1] if targets else label)
            if role is None:
                continue
            if not targets:
                issue(source, "unmatched_reference")
            elif len({c.section_id for c in targets}) > 1:
                issue(source, "ambiguous_reference")
            else:
                add(source, targets, role, "parser_reference", "parser:explicit-section-reference", match[0])
    validate_evidence_links(chunks)
