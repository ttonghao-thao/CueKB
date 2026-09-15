import pytest

from cuekb.services.parsing import ParseError, parse_document


def test_markdown_preserves_heading_and_offsets() -> None:
    content = "# 故障处理\n\nE102 表示链路异常。\n\n## 步骤\n检查光功率。"
    blocks = parse_document(content.encode(), "manual.md", "text/markdown")
    assert [block.text for block in blocks] == ["E102 表示链路异常。", "检查光功率。"]
    assert blocks[0].title_path == ["故障处理"]
    assert blocks[1].title_path == ["故障处理", "步骤"]
    for block in blocks:
        assert content[block.anchor.start_offset : block.anchor.end_offset] == block.text


def test_empty_text_is_rejected() -> None:
    with pytest.raises(ParseError, match="empty_content"):
        parse_document(b" \n\n", "empty.txt", "text/plain")
