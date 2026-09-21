from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from pathlib import Path

from cuekb.domain.models import SourceAnchor


@dataclass(frozen=True)
class ParsedBlock:
    text: str
    title_path: list[str]
    anchor: SourceAnchor


class ParseError(RuntimeError):
    def __init__(self, code: str, *, retryable: bool = False) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable


def parse_document(content: bytes, filename: str, media_type: str | None) -> list[ParsedBlock]:
    suffix = Path(filename).suffix.lower()
    if suffix in {".txt", ".md", ".markdown"} or media_type in {"text/plain", "text/markdown"}:
        return _parse_text(content, markdown=suffix in {".md", ".markdown"})
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
        blocks: list[ParsedBlock] = []
        for item, level in result.document.iterate_items(traverse_pictures=True):
            label = getattr(getattr(item, "label", None), "value", "")
            if isinstance(item, TextItem) and label in {"title", "section_header"}:
                headings = headings[: max(level - 1, 0)] + [item.text.strip()]
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
            blocks.extend(_split_block(value, headings, anchor))
    except Exception as exc:
        raise ParseError("document_parse_failed", retryable=True) from exc
    finally:
        temporary.unlink(missing_ok=True)
    if not blocks:
        raise ParseError("empty_content")
    return blocks


def _parse_text(content: bytes, markdown: bool) -> list[ParsedBlock]:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ParseError("invalid_utf8") from exc
    if not text.strip():
        raise ParseError("empty_content")
    if text.count("\ufffd") / max(len(text), 1) > 0.01:
        raise ParseError("replacement_character_ratio_high")
    blocks: list[ParsedBlock] = []
    headings: list[str] = []
    pending: list[str] = []
    pending_start = 0
    offset = 0
    fenced = False

    def flush() -> None:
        value = "".join(pending).strip()
        if value:
            raw = "".join(pending)
            start = pending_start + len(raw) - len(raw.lstrip())
            blocks.extend(
                _split_block(
                    value,
                    headings,
                    SourceAnchor(
                        heading_path=headings.copy(),
                        start_offset=start,
                        end_offset=start + len(value),
                    ),
                )
            )
        pending.clear()

    for line in text.splitlines(keepends=True):
        heading = (
            re.match(r"^ {0,3}(#{1,6})\s+(.+?)\s*#*\s*$", line) if markdown and not fenced else None
        )
        if heading:
            flush()
            level = len(heading[1])
            headings = headings[: level - 1] + [heading[2]]
        elif not line.strip() and not fenced:
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


def _split_block(
    value: str, headings: list[str], anchor: SourceAnchor, max_chars: int = 1200
) -> list[ParsedBlock]:
    lines = value.splitlines()
    if len(lines) > 2 and "|" in lines[0] and re.match(r"^\s*\|?[\s:|\-]+\|?\s*$", lines[1]):
        header = "\n".join(lines[:2])
        if len(value) > max_chars and len(header) < max_chars // 2:
            result: list[ParsedBlock] = []
            rows: list[str] = []
            for row in lines[2:]:
                # Keep units and column labels with every split row.
                for start in range(0, max(len(row), 1), max_chars - len(header) - 1):
                    piece = row[start : start + max_chars - len(header) - 1]
                    if (
                        rows
                        and len(header) + sum(len(r) + 1 for r in rows) + len(piece) + 1 > max_chars
                    ):
                        result.append(
                            ParsedBlock(header + "\n" + "\n".join(rows), headings.copy(), anchor)
                        )
                        rows = []
                    rows.append(piece)
            if rows:
                result.append(ParsedBlock(header + "\n" + "\n".join(rows), headings.copy(), anchor))
            return result
    if len(value) <= max_chars:
        return [ParsedBlock(value, headings.copy(), anchor)]
    result = []
    for start in range(0, len(value), max_chars):
        item = value[start : start + max_chars]
        child = anchor.model_copy(
            update={
                "start_offset": anchor.start_offset + start
                if anchor.start_offset is not None
                else None,
                "end_offset": min(anchor.start_offset + start + len(item), anchor.end_offset)
                if anchor.start_offset is not None and anchor.end_offset is not None
                else None,
            }
        )
        result.append(ParsedBlock(item, headings.copy(), child))
    return result
