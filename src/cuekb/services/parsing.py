from __future__ import annotations

import re
import hashlib
import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID

from cuekb.domain.models import EvidenceStructure, SourceAnchor, SourceSpan
from cuekb.services.evidence import PROCESSING_REVISION, source_unit_id, structure_for


@dataclass(frozen=True)
class ParsedBlock:
    text: str
    title_path: list[str]
    anchor: SourceAnchor
    source_spans: list[SourceSpan] = field(default_factory=list)
    evidence_structure: EvidenceStructure = field(default_factory=EvidenceStructure)
    # Parser-local aliases used only to relocate administrator bindings from a
    # source version. The new chunk always stores its new version-bound unit ID.
    source_unit_aliases: tuple[str, ...] = ()


class ParseError(RuntimeError):
    def __init__(self, code: str, *, retryable: bool = False) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable


def parse_document(
    content: bytes, filename: str, media_type: str | None, *,
    processing_profile: str = "evidence-v2",
    processing_snapshot: dict[str, Any] | None = None,
    version_id: UUID | None = None,
) -> list[ParsedBlock]:
    if processing_profile != "evidence-v2" and not re.fullmatch(r"evidence-v2:[0-9a-f]{64}", processing_profile):
        raise ParseError("unsupported_processing_profile")
    if processing_profile != "evidence-v2":
        encoded = json.dumps(processing_snapshot or {}, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        if processing_profile != "evidence-v2:" + hashlib.sha256(encoded.encode()).hexdigest():
            raise ParseError("processing_snapshot_hash_mismatch")
    if processing_snapshot and any(
        processing_snapshot.get(key, PROCESSING_REVISION) != PROCESSING_REVISION
        for key in ("parser_revision", "chunker_revision", "structure_revision")
    ):
        raise ParseError("unsupported_frozen_processing_revision")
    suffix = Path(filename).suffix.lower()
    if suffix in {".txt", ".md", ".markdown"} or media_type in {"text/plain", "text/markdown"}:
        blocks = _parse_text_v2(
            content, markdown=suffix in {".md", ".markdown"} or media_type == "text/markdown",
            snapshot=processing_snapshot, version_id=version_id,
        )
        _validate_scope_bindings(blocks, processing_snapshot)
        return blocks
    if suffix not in {".pdf", ".docx"}:
        raise ParseError("unsupported_file_type")
    try:
        from docling.datamodel.base_models import InputFormat
        from docling.datamodel.pipeline_options import PdfPipelineOptions
        from docling.document_converter import DocumentConverter, PdfFormatOption
        from docling_core.types.doc import TableItem, TextItem
    except ImportError as exc:
        raise ParseError("docling_not_installed") from exc

    temporary = Path("/tmp") / f"cuekb-parse-{uuid.uuid4().hex}{suffix}"
    temporary.write_bytes(content)
    try:
        options = PdfPipelineOptions()
        options.do_ocr = True
        options.do_table_structure = True
        converter = DocumentConverter(
            format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options)}
        )
        result = converter.convert(temporary)
        headings: list[str] = []
        heading_spans: list[SourceSpan] = []
        blocks: list[ParsedBlock] = []
        for item_index, (item, level) in enumerate(result.document.iterate_items(traverse_pictures=True)):
            label = getattr(getattr(item, "label", None), "value", "")
            if isinstance(item, TextItem) and label in {"title", "section_header"}:
                headings = headings[: max(level - 1, 0)] + [item.text.strip()]
                heading_spans = heading_spans[: max(level - 1, 0)] + _item_spans(
                    item, item.text.strip(), headings, "heading"
                )[:1]
                continue
            if isinstance(item, TextItem):
                value = item.text.strip()
            elif isinstance(item, TableItem):
                value = item.export_to_markdown(result.document).strip()
            else:
                continue
            if not value:
                continue
            provenance = item.prov[0] if getattr(item, "prov", None) else None
            anchor = SourceAnchor(
                page=getattr(provenance, "page_no", None),
                heading_path=headings.copy(),
                start_offset=provenance.charspan[0] if provenance else None,
                end_offset=provenance.charspan[1] if provenance else None,
                bbox=provenance.bbox.model_dump() if provenance else None,
            )
            spans = _item_spans(item, value, headings, "table" if isinstance(item, TableItem) else "text")
            blocks.extend(_split_block_v2(
                value, headings, anchor, heading_spans=heading_spans,
                snapshot=processing_snapshot, version_id=version_id,
                identity=f"docling:{getattr(item, 'self_ref', item_index)}",
                original_spans=spans,
            ))
    except Exception as exc:
        raise ParseError("document_parse_failed", retryable=True) from exc
    finally:
        temporary.unlink(missing_ok=True)
    if not blocks:
        raise ParseError("empty_content")
    _validate_scope_bindings(blocks, processing_snapshot)
    return blocks


def _item_spans(item: Any, text: str, headings: list[str], label: str) -> list[SourceSpan]:
    """Keep every actual Docling provenance; do not invent page/cell mappings."""
    spans = []
    for index, provenance in enumerate(getattr(item, "prov", []) or [None]):
        charspan = getattr(provenance, "charspan", None)
        bbox = getattr(provenance, "bbox", None)
        span_text = text
        if label != "table" and charspan and 0 <= charspan[0] < charspan[1] <= len(text):
            span_text = text[charspan[0]:charspan[1]]
        spans.append(SourceSpan(
            label=label, text=span_text[:32768], parser_item_id=str(getattr(item, "self_ref", "")) or None,
            span_id=f"prov:{index}",
            anchor=SourceAnchor(
                page=getattr(provenance, "page_no", None), heading_path=headings.copy(),
                start_offset=charspan[0] if charspan else None,
                end_offset=charspan[1] if charspan else None,
                bbox=bbox.model_dump() if bbox else None,
            ),
        ))
    return spans


def _parse_text_v2(
    content: bytes, *, markdown: bool, snapshot: dict[str, Any] | None, version_id: UUID | None,
) -> list[ParsedBlock]:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ParseError("invalid_utf8") from exc
    if not text.strip():
        raise ParseError("empty_content")
    if text.count("\ufffd") / max(len(text), 1) > 0.01:
        raise ParseError("replacement_character_ratio_high")
    blocks, headings, heading_spans = [], [], []
    pending: list[str] = []
    pending_start, offset, fenced = 0, 0, False

    def flush() -> None:
        raw = "".join(pending)
        value = raw.strip()
        if value:
            start = pending_start + len(raw) - len(raw.lstrip())
            blocks.extend(_split_block_v2(
                value, headings, SourceAnchor(
                    heading_path=headings.copy(), start_offset=start, end_offset=start + len(value)
                ), heading_spans=heading_spans, snapshot=snapshot, version_id=version_id,
                identity=f"text:{start}",
            ))
        pending.clear()

    text_lines = text.splitlines(keepends=True)
    for line_index, line in enumerate(text_lines):
        heading = re.match(r"^ {0,3}(#{1,6})\s+(.+?)\s*#*\s*$", line) if markdown and not fenced else None
        if heading:
            flush()
            depth = len(heading[1])
            headings = headings[:depth - 1] + [heading[2]]
            title_start = offset + heading.start(2)
            heading_spans = heading_spans[:depth - 1] + [SourceSpan(
                label="heading", text=heading[2], span_id=f"heading:{title_start}",
                anchor=SourceAnchor(
                    heading_path=headings.copy(), start_offset=title_start,
                    end_offset=title_start + len(heading[2]),
                ),
            )]
        elif not line.strip() and not fenced:
            next_line = next((entry for entry in text_lines[line_index + 1:] if entry.strip()), "")
            if pending and "".join(pending).rstrip().endswith((":", "：")) and re.match(
                r"\s*(?:[-*+]\s|\d+[.)]\s)", next_line
            ):
                # The introductory condition and its immediately following
                # list remain one original contiguous semantic unit.
                pending.append(line)
            else:
                flush()
        else:
            if not pending:
                pending_start = offset
            pending.append(line)
        if markdown and line.lstrip().startswith(("```", "~~~")):
            fenced = not fenced
        offset += len(line)
    flush()
    if not blocks:
        raise ParseError("empty_content")
    return blocks


def _offset_anchor(anchor: SourceAnchor, start: int, length: int) -> SourceAnchor:
    return anchor.model_copy(update={
        "start_offset": anchor.start_offset + start if anchor.start_offset is not None else None,
        "end_offset": anchor.start_offset + start + length if anchor.start_offset is not None else None,
    })


def _safe_ranges(value: str, max_chars: int) -> list[tuple[int, int, bool]]:
    """Prefer whole sentences/list lines; any unavoidable split is explicitly partial."""
    boundaries = [0]
    boundaries.extend(match.end() for match in re.finditer(r"(?<=[.!?。！？])\s+|\n", value))
    boundaries.append(len(value))
    ranges: list[tuple[int, int, bool]] = []
    start = 0
    while start < len(value):
        choices = [b for b in boundaries if start < b <= start + max_chars]
        end = max(choices) if choices else min(start + max_chars, len(value))
        partial = not choices and end < len(value)
        if partial:
            space = value.rfind(" ", start + max_chars // 2, end)
            if space > start:
                end = space + 1
        # A following fragment of an overlong semantic unit is also incomplete.
        partial = partial or start not in boundaries
        ranges.append((start, end, partial))
        start = end
    return ranges


def _split_block_v2(
    value: str, headings: list[str], anchor: SourceAnchor, *,
    heading_spans: list[SourceSpan], snapshot: dict[str, Any] | None,
    version_id: UUID | None, identity: str, original_spans: list[SourceSpan] | None = None,
    max_chars: int = 1200,
) -> list[ParsedBlock]:
    unit_id = source_unit_id(identity, version_id)
    origin_version = snapshot.get("origin_version_id") if snapshot else None
    mapped = (snapshot or {}).get("origin_unit_aliases", {}).get(identity)
    aliases = (mapped,) if mapped else (source_unit_id(identity, origin_version),) if origin_version else ()
    lines = value.splitlines(keepends=True)
    is_table = len(lines) > 2 and "|" in lines[0] and re.match(r"^\s*\|?[\s:|\-]+\|?\s*$", lines[1])
    if is_table:
        return _split_table_v2(
            value, headings, anchor, heading_spans=heading_spans, snapshot=snapshot,
            unit_id=unit_id, max_chars=max_chars, original_spans=original_spans,
            parser_unit_key=identity,
            version_id=version_id,
        )
    kind = "list" if re.match(r"\s*(?:[-*+]\s|\d+[.)]\s)", value) else "text"
    ranges = _safe_ranges(value, max_chars)
    result = []
    for start, end, partial in ranges:
        piece = value[start:end]
        if not piece.strip():
            continue
        child = _offset_anchor(anchor, start, len(piece)) if not original_spans else anchor
        spans = list(heading_spans)
        if original_spans:
            spans.extend(original_spans)
        else:
            spans.append(SourceSpan(label=kind, text=piece, anchor=child, span_id=f"text:{start}"))
        unresolved = partial or len(spans) > 64
        spans = spans[:64]
        result.append(ParsedBlock(
            piece, headings.copy(), child, spans,
            structure_for(block_kind=kind, unit_id=unit_id, title_path=headings,
                          source_spans=spans, snapshot=snapshot, unresolved=unresolved,
                          source_unit_aliases=aliases, parser_unit_key=identity), aliases,
        ))
    return result


def _split_table_v2(
    value: str, headings: list[str], anchor: SourceAnchor, *, heading_spans: list[SourceSpan],
    snapshot: dict[str, Any] | None, unit_id: str, max_chars: int,
    original_spans: list[SourceSpan] | None,
    parser_unit_key: str | None = None, version_id: UUID | None = None,
) -> list[ParsedBlock]:
    lines = value.splitlines(keepends=True)
    header_raw = "".join(lines[:2])
    header = header_raw.rstrip("\r\n")
    header_span = SourceSpan(label="table_header", text=header[:32768],
                             anchor=anchor if original_spans else _offset_anchor(anchor, 0, len(header)))
    rows: list[tuple[str, list[SourceSpan], bool]] = []
    offset = len(header_raw)
    for row_index, raw in enumerate(lines[2:], start=1):
        row = raw.rstrip("\r\n")
        cell_spans = []
        # Escaped pipes are cell text, not delimiters. Spans preserve exact text.
        cells = list(re.finditer(r"(?:\\.|[^|])+", row))
        for column_index, cell in enumerate(cells):
            if cell.group().strip():
                cell_spans.append(SourceSpan(
                    label="table_row_label" if column_index == 0 else "table_cell",
                    text=cell.group()[:32768], row_index=row_index,
                    column_index=column_index,
                    anchor=anchor if original_spans else _offset_anchor(anchor, offset + cell.start(), len(cell.group())),
                ))
        room = max(1, max_chars - len(header) - 1)
        if len(row) <= room:
            rows.append((row, cell_spans, False))
        else:
            # Prefer complete cells. A single overlong cell uses continuation
            # fragments and remains unresolved; it is never declared standalone.
            starts = [0] + [match.end() for match in re.finditer(r"(?<!\\)\|", row)]
            starts = sorted(set(starts + [len(row)]))
            position = 0
            while position < len(row):
                ends = [end for end in starts if position < end <= position + room]
                end = max(ends) if ends else min(position + room, len(row))
                piece = row[position:end]
                piece_span = SourceSpan(
                    label="table_continuation", text=piece[:32768], row_index=row_index,
                    anchor=anchor if original_spans else _offset_anchor(anchor, offset + position, len(piece)),
                )
                rows.append((piece, [piece_span], True))
                position = end
        offset += len(raw)
    result, grouped, used = [], [], len(header)

    def emit() -> None:
        if not grouped:
            return
        body = header + "\n" + "\n".join(row[0] for row in grouped)
        spans = [*heading_spans, header_span]
        spans.extend(span for row in grouped for span in row[1])
        if original_spans:
            spans.extend(original_spans)
        unresolved = (any(row[2] for row in grouped) or len(spans) > 64
                      or bool(original_spans and len(original_spans) > 1)
                      or len(header) > 32768)
        # A row is a distinct scope selector, not just a distinct returned fact.
        # Otherwise mapping one product row would silently bind the whole table.
        row_index = next((span.row_index for row in grouped for span in row[1]), 0)
        key = f"{parser_unit_key or unit_id}:row:{row_index}"
        row_unit = source_unit_id(key, version_id)
        mapped = (snapshot or {}).get("origin_unit_aliases", {}).get(key)
        origin = (snapshot or {}).get("origin_version_id")
        aliases = (mapped,) if mapped else (source_unit_id(key, origin),) if origin else ()
        result.append(ParsedBlock(
            body, headings.copy(), anchor, spans[:64],
            structure_for(block_kind="table", unit_id=row_unit, title_path=headings,
                          source_spans=spans[:64], snapshot=snapshot, unresolved=unresolved,
                          source_unit_aliases=aliases, parser_unit_key=key), aliases,
        ))
        grouped.clear()

    for row in rows:
        # A row is the smallest safe table scope: batching two products into a
        # single fact would make both products appear to own every cell.
        if grouped:
            emit()
            used = len(header)
        grouped.append(row)
        used += 1 + len(row[0])
        if row[2]:
            emit()
            used = len(header)
    emit()
    return result


def _validate_scope_bindings(blocks: list[ParsedBlock], snapshot: dict[str, Any] | None) -> None:
    for binding in (snapshot or {}).get("scope_bindings", []):
        path, unit = binding.get("title_path"), binding.get("source_unit_id")
        matched = any(
            (path is None or (bool(path) and block.title_path[:len(path)] == path))
            and (unit is None or unit in {
                block.evidence_structure.source_unit_id, *block.source_unit_aliases
            })
            for block in blocks
        )
        if not matched:
            raise ParseError("unresolved_scope_binding")
