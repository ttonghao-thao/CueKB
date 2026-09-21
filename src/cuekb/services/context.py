"""Deterministic section hierarchy and bounded, attributable context."""

from collections.abc import Sequence
from uuid import uuid5

from cuekb.domain.models import Chunk, Section
from cuekb.schemas import ContextPart


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


def assemble_context(
    hit: Chunk, neighbors: Sequence[Chunk], budget: int
) -> tuple[str, list[ContextPart], bool]:
    eligible = {
        c.id: c
        for c in neighbors
        if c.version_id == hit.version_id and c.document_id == hit.document_id
    }
    eligible[hit.id] = hit
    priority = sorted(
        eligible.values(), key=lambda c: (c.id != hit.id, abs(c.ordinal - hit.ordinal), c.ordinal)
    )
    chosen: list[Chunk] = []
    used = 0
    for chunk in priority:
        cost = len(chunk.source_text) + (2 if chosen else 0)
        if used + cost <= budget:
            chosen.append(chunk)
            used += cost
    chosen.sort(key=lambda c: c.ordinal)
    parts = [
        ContextPart(
            chunk_id=c.id, source_text=c.source_text, anchor=c.anchor, title_path=c.title_path
        )
        for c in chosen
    ]
    return "\n\n".join(c.source_text for c in chosen), parts, len(chosen) < len(eligible)
