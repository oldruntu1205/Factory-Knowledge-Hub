"""解析器测试。

测试文件全部在运行时生成（PDF 手工拼装、DOCX 用 python-docx 写入），
仓库里不存二进制样例：既避免体积膨胀，也避免把真实客户资料带进版本库。
"""

from __future__ import annotations

import io

import pytest
from docx import Document

from fkh.domain.parsing.csv_parser import parse_csv
from fkh.domain.parsing.docx import parse_docx
from fkh.domain.parsing.html import parse_html
from fkh.domain.parsing.markdown import parse_markdown
from fkh.domain.parsing.pdf import parse_pdf
from fkh.domain.parsing.registry import UnsupportedFormatError, parse_document, resolve_parser
from fkh.domain.parsing.types import BlockKind, ReviewReason

SHA = "0" * 64


# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------
MD_SAMPLE = """# 冷凝管产品说明

高硼硅玻璃冷凝管适用于回流与蒸馏。接口规格需按客户设备确认。

## 规格表

| 型号 | 接口 | 长度 | 单位 |
| --- | --- | --- | --- |
| 24/29 | 24/29 | 300 | mm |
| 29/32 | 29/32 | 400 | mm |

## 注意事项

- 不得用于氢氟酸环境
- 升温速率建议不超过 3 ℃/min
"""


def test_markdown_keeps_heading_levels() -> None:
    doc = parse_markdown(MD_SAMPLE.encode(), source_id="s1", source_sha256=SHA)
    headings = [(b.text, b.level) for b in doc.blocks if b.kind is BlockKind.HEADING]
    assert headings == [("冷凝管产品说明", 1), ("规格表", 2), ("注意事项", 2)]


def test_markdown_keeps_table_headers() -> None:
    doc = parse_markdown(MD_SAMPLE.encode(), source_id="s1", source_sha256=SHA)
    tables = [b for b in doc.blocks if b.kind is BlockKind.TABLE]
    assert len(tables) == 1
    table = tables[0].table
    assert table is not None
    assert table.headers == ("型号", "接口", "长度", "单位")
    assert table.rows[0] == ("24/29", "24/29", "300", "mm")


def test_markdown_keeps_list_items() -> None:
    doc = parse_markdown(MD_SAMPLE.encode(), source_id="s1", source_sha256=SHA)
    lists = [b for b in doc.blocks if b.kind is BlockKind.LIST]
    assert lists
    assert "不得用于氢氟酸环境" in lists[0].text


def test_markdown_keeps_inline_html_as_text() -> None:
    """html=False 只阻止 HTML 被解析成结构，不做内容清洗。

    这是刻意的边界：注入防护属于检索与生成层（L1 §8.4），解析器若在此"顺手清洗"，
    会让人误以为安全边界已经前移，反而放松下游检查。
    """
    payload = "# 标题\n\n<script>alert(1)</script>\n\n正文内容。\n"
    doc = parse_markdown(payload.encode(), source_id="s1", source_sha256=SHA)
    rendered = "\n".join(block.text for block in doc.blocks)
    assert "正文内容。" in rendered
    assert all(block.kind is not BlockKind.CODE for block in doc.blocks)
    assert "alert(1)" in rendered


def test_markdown_empty_document_flagged() -> None:
    doc = parse_markdown(b"   \n\n", source_id="s1", source_sha256=SHA)
    assert ReviewReason.EMPTY_DOCUMENT in doc.review_reasons
    assert doc.needs_human_review


# ---------------------------------------------------------------------------
# CSV
# ---------------------------------------------------------------------------
def test_csv_headers_and_rows() -> None:
    payload = "型号,接口,长度(mm)\n24/29,24/29,300\n29/32,29/32,400\n".encode()
    doc = parse_csv(payload, source_id="s1", source_sha256=SHA)
    table = doc.blocks[0].table
    assert table is not None
    assert table.headers == ("型号", "接口", "长度(mm)")
    assert len(table.rows) == 2


def test_csv_without_header_is_flagged() -> None:
    payload = b"24/29,300,4.2\n29/32,400,5.1\n"
    doc = parse_csv(payload, source_id="s1", source_sha256=SHA)
    assert ReviewReason.TABLE_STRUCTURE_UNCERTAIN in doc.review_reasons


def test_csv_ragged_rows_flagged() -> None:
    payload = b"a,b,c\n1,2,3\n4,5\n"
    doc = parse_csv(payload, source_id="s1", source_sha256=SHA)
    assert ReviewReason.TABLE_STRUCTURE_UNCERTAIN in doc.review_reasons


def test_csv_gbk_encoded_file_decoded() -> None:
    """国内资料常见 GBK；解码失败会整份文档不可用。"""
    payload = "型号,接口\n冷凝管,24/29\n".encode("gb18030")
    doc = parse_csv(payload, source_id="s1", source_sha256=SHA)
    table = doc.blocks[0].table
    assert table is not None
    assert table.headers == ("型号", "接口")


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------
HTML_SAMPLE = """
<html><head><style>.x{color:red}</style><script>var a=1</script></head>
<body>
<h1>烧瓶技术参数</h1>
<p>圆底烧瓶用于加热回流。</p>
<ul><li>容积 250 mL</li><li>磨口 24/29</li></ul>
<table>
  <tr><th>型号</th><th>容积</th></tr>
  <tr><td>FB-250</td><td>250 mL</td></tr>
</table>
</body></html>
"""


def test_html_extracts_structure() -> None:
    doc = parse_html(HTML_SAMPLE.encode(), source_id="s1", source_sha256=SHA)
    kinds = [block.kind for block in doc.blocks]
    assert BlockKind.HEADING in kinds
    assert BlockKind.PARAGRAPH in kinds
    assert BlockKind.LIST in kinds
    assert BlockKind.TABLE in kinds
    rendered = "\n".join(block.text for block in doc.blocks)
    assert "var a=1" not in rendered
    assert "color:red" not in rendered


def test_html_merged_cells_flagged() -> None:
    payload = b"<table><tr><th colspan='2'>\xe5\x90\x88\xe5\xb9\xb6\xe8\xa1\xa8\xe5\xa4\xb4</th></tr><tr><td>1</td><td>2</td></tr></table>"
    doc = parse_html(payload, source_id="s1", source_sha256=SHA)
    assert ReviewReason.TABLE_STRUCTURE_UNCERTAIN in doc.review_reasons


# ---------------------------------------------------------------------------
# DOCX
# ---------------------------------------------------------------------------
def _docx_bytes() -> bytes:
    document = Document()
    document.add_heading("冷凝管检验报告", level=1)
    document.add_paragraph("本批产品接口规格均为 24/29，需逐件核对。")
    table = document.add_table(rows=3, cols=3)
    headers = ("批号", "接口", "结论")
    for index, text in enumerate(headers):
        table.cell(0, index).text = text
    table.cell(1, 0).text = "B20261101"
    table.cell(1, 1).text = "24/29"
    table.cell(1, 2).text = "合格"
    table.cell(2, 0).text = "B20261102"
    table.cell(2, 1).text = "29/32"
    table.cell(2, 2).text = "合格"

    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def test_docx_heading_and_table_in_order() -> None:
    doc = parse_docx(_docx_bytes(), source_id="s1", source_sha256=SHA)
    kinds = [block.kind for block in doc.blocks]
    assert kinds[0] is BlockKind.HEADING
    assert BlockKind.TABLE in kinds
    # 表格必须出现在段落之后（原始顺序），而不是被移到文档末尾
    assert kinds.index(BlockKind.TABLE) > kinds.index(BlockKind.PARAGRAPH)


def test_docx_table_headers() -> None:
    doc = parse_docx(_docx_bytes(), source_id="s1", source_sha256=SHA)
    table_block = next(block for block in doc.blocks if block.kind is BlockKind.TABLE)
    assert table_block.table is not None
    assert table_block.table.headers == ("批号", "接口", "结论")
    assert len(table_block.table.rows) == 2


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------
def _minimal_pdf(lines: list[str]) -> bytes:
    content = ["BT", "/F1 12 Tf", "72 720 Td", "14 TL"]
    for index, line in enumerate(lines):
        escaped = line.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
        if index:
            content.append("T*")
        content.append(f"({escaped}) Tj")
    content.append("ET")
    stream = "\n".join(content).encode("latin-1")

    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]

    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"

    xref_at = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_at}\n%%EOF\n"
    ).encode()
    return bytes(out)


def test_pdf_extracts_lines_with_page_locator() -> None:
    doc = parse_pdf(
        _minimal_pdf(["Condenser 24/29 300mm", "Beaker 250 mL"]),
        source_id="s1",
        source_sha256=SHA,
    )
    assert doc.page_count == 1
    texts = [block.text for block in doc.blocks]
    assert any("Condenser" in text for text in texts)
    assert all(block.locator.page_start == 1 for block in doc.blocks)
    assert all(block.locator.bbox is not None for block in doc.blocks)


def test_pdf_without_text_flagged_for_ocr() -> None:
    """扫描件没有可提取文本，必须显式标记，不能静默产出空文档。"""
    doc = parse_pdf(_minimal_pdf([]), source_id="s1", source_sha256=SHA)
    assert ReviewReason.PARSER_FALLBACK in doc.review_reasons
    assert ReviewReason.EMPTY_DOCUMENT in doc.review_reasons


# ---------------------------------------------------------------------------
# 分派
# ---------------------------------------------------------------------------
def test_registry_dispatches_by_suffix() -> None:
    assert resolve_parser(filename="a.md") is parse_markdown
    assert resolve_parser(filename="a.PDF") is parse_pdf
    assert resolve_parser(filename="a.docx") is parse_docx


def test_registry_prefers_mime() -> None:
    assert resolve_parser(filename="mystery", mime_type="text/csv") is parse_csv


@pytest.mark.parametrize("filename", ["a.doc", "b.xlsx", "c.zip", "noext"])
def test_registry_rejects_unsupported(filename: str) -> None:
    with pytest.raises(UnsupportedFormatError):
        resolve_parser(filename=filename)


def test_parse_document_computes_sha256() -> None:
    doc = parse_document(MD_SAMPLE.encode(), source_id="s1", filename="a.md")
    assert doc.source_sha256 != "0" * 64
    assert doc.parser_id.startswith("markdown@")
