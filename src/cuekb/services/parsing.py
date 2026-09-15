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
    offset = 0
    for part in re.finditer(r"\S(?:.*?\S)?(?=\n\s*\n|\Z)", text, re.DOTALL):
        value = part.group(0).strip()
        if markdown and value.startswith("#"):
            first, _, remainder = value.partition("\n")
            level = len(first) - len(first.lstrip("#"))
            title = first[level:].strip()
            headings = headings[: max(level - 1, 0)] + ([title] if title else [])
            value = remainder.strip()
            if not value:
                continue
        start = text.find(value, max(offset, part.start()))
        end = start + len(value)
        offset = end
        blocks.extend(
            _split_block(
                value,
                headings,
                SourceAnchor(heading_path=headings.copy(), start_offset=start, end_offset=end),
            )
        )
    if not blocks:
        raise ParseError("empty_content")
    return blocks


def _split_block(
    value: str, headings: list[str], anchor: SourceAnchor, max_chars: int = 1200
) -> list[ParsedBlock]:
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
