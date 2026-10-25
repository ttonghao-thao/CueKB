"""Deterministic section hierarchy and bounded, attributable context."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal
from uuid import UUID, uuid5

from cuekb.domain.models import Chunk, ContextRole, Section, SourceAnchor, SourceSpan, StructureModel
from cuekb.services.evidence import scope_compatible


def build_sections(chunks: Sequence[Chunk]) -> list[Section]:
    sections: list[Section] = []
    active: list[Section] = []
    previous: list[str] = []
    for chunk in chunks:
        path = chunk.title_path
        if not active:
            root = Section(
                id=uuid5(chunk.version_id, "section:root"),
                version_id=chunk.version_id,
                ordinal=0,
                title_path=[],
                content="",
            )
            sections.append(root)
            active.append(root)
        common = 0
        while common < min(len(path), len(previous)) and path[common] == previous[common]:
            common += 1
        active = active[: common + 1]
        for depth in range(common + 1, len(path) + 1):
            section = Section(
                id=uuid5(chunk.version_id, f"section:{chunk.ordinal}:{path[:depth]!r}"),
                version_id=chunk.version_id,
                parent_id=active[-1].id,
                ordinal=len(sections),
                title_path=path[:depth],
                content="",
            )
            sections.append(section)
            active.append(section)
        chunk.section_id = active[-1].id
        previous = path
    # Direct content only: ancestors do not duplicate the whole document.
    by_section: dict = {}
    for chunk in chunks:
        by_section.setdefault(chunk.section_id, []).append(chunk.source_text)
    for section in sections:
        section.content = "\n\n".join(by_section.get(section.id, []))
    return sections


class EvidenceContextPart(StructureModel):
    chunk_id: UUID
    source_text: str
    anchor: SourceAnchor
    title_path: list[str]
    source_spans: list[SourceSpan]
    role: ContextRole | None = None
    required: bool = False


@dataclass(frozen=True)
class ContextAssembly:
    context: str
    parts: list[EvidenceContextPart]
    state: Literal["disabled", "complete", "truncated", "unresolved"]
    omissions: list[str]
    truncated: bool


def assemble_context_v2(
    hit: Chunk, neighbors: Sequence[Chunk], budget: int, *,
    include_optional: bool = True, max_chunks: int = 8, required_only: bool = False,
) -> ContextAssembly:
    """One-hop, whole-chunk context. Call for required groups before optional allocation.

    All neighbors must have been read in the same authority snapshot as ``hit``.
    This selector still checks identity and scope, and never follows a second hop.
    The separate hit.source_text is not modified even when its context cannot fit.
    """
    budget, max_chunks = max(0, budget), min(20, max(1, max_chunks))
    by_id = {chunk.id: chunk for chunk in neighbors}
    by_id[hit.id] = hit
    links = hit.evidence_structure.context_links
    required = [link for link in links if link.required]
    omissions: set[str] = set()
    unresolved = hit.evidence_structure.dependency_unresolved
    if unresolved:
        omissions.add("dependency_unresolved")
    if not required and not include_optional:
        return ContextAssembly("", [], "unresolved" if unresolved else "disabled",
                               sorted(omissions), False)
    selected: dict = {}
    used, truncated = 0, False

    def choose(chunk: Chunk, role: ContextRole | None, necessary: bool) -> bool:
        nonlocal used, truncated
        if chunk.id in selected:
            return True
        cost = len(chunk.source_text) + (2 if selected else 0)
        if len(selected) >= max_chunks:
            omissions.add("candidate_limit_reached")
            truncated = True
            return False
        if used + cost > budget:
            omissions.add("budget_omitted")
            truncated = True
            return False
        selected[chunk.id] = (chunk, role, necessary)
        used += cost
        return True

    # The fact is included in context only as its true persistent chunk identity.
    choose(hit, None, True)
    visited = {hit.id}
    direct_ids = {hit.id, *(link.chunk_id for link in required)}
    for link in required[:20]:
        target = by_id.get(link.chunk_id)
        if link.chunk_id in visited or target is None:
            unresolved = True
            omissions.add("dependency_unresolved")
            continue
        visited.add(link.chunk_id)
        if not scope_compatible(hit, target, required=True, role=link.role):
            unresolved = True
            omissions.update(("unsafe_scope_excluded", "dependency_unresolved"))
            continue
        if not choose(target, link.role, True):
            unresolved = True
            omissions.add("dependency_unresolved")
        # The import must flatten necessary relationships. A second-hop need
        # cannot be silently certified by a one-hop selector.
        if target.evidence_structure.dependency_unresolved or any(
            other.required and other.chunk_id not in direct_ids
            for other in target.evidence_structure.context_links
        ):
            unresolved = True
            omissions.add("dependency_unresolved")
    if include_optional and not required_only:
        linked = {link.chunk_id: link for link in links if not link.required}
        eligible = []
        for candidate in by_id.values():
            if candidate.id in visited:
                continue
            unit = hit.evidence_structure.source_unit_id
            same_unit = bool(unit and unit == candidate.evidence_structure.source_unit_id)
            same_section = (hit.section_id == candidate.section_id and hit.title_path == candidate.title_path)
            nearby = same_section and abs(candidate.ordinal - hit.ordinal) <= 1
            if not (candidate.id in linked or same_unit or nearby):
                continue
            link = linked.get(candidate.id)
            if not scope_compatible(hit, candidate, role=link.role if link else None):
                omissions.add("unsafe_scope_excluded")
                continue
            if candidate.evidence_structure.dependency_unresolved or any(
                dependency.required and dependency.chunk_id not in selected
                for dependency in candidate.evidence_structure.context_links
            ):
                # An optional fragment with unmet dependencies is not a safe
                # standalone neighbor. Excluding it does not make the fact's
                # own already-complete required group incomplete.
                omissions.add("dependency_unresolved")
                continue
            eligible.append((candidate, link, same_unit))
        eligible.sort(key=lambda item: (
            item[1] is None, not item[2], abs(item[0].ordinal - hit.ordinal), str(item[0].id)
        ))
        for candidate, link, _ in eligible:
            choose(candidate, link.role if link else None, False)
    ordered = sorted(selected.values(), key=lambda item: (item[0].ordinal, str(item[0].id)))
    parts = [EvidenceContextPart(
        chunk_id=chunk.id, source_text=chunk.source_text, anchor=chunk.anchor,
        title_path=chunk.title_path, source_spans=chunk.source_spans, role=role, required=necessary,
    ) for chunk, role, necessary in ordered]
    state = "unresolved" if unresolved else "truncated" if truncated else "complete"
    return ContextAssembly("\n\n".join(part.source_text for part in parts), parts,
                           state, sorted(omissions), truncated)
