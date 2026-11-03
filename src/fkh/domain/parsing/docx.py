"""DOCX 解析。

工厂的技术说明、SOP、检验报告多为 Word。python-docx 能按文档顺序同时给出段落与表格，
这一点很关键：先读全部段落再读全部表格会把"表 3 适用于 XX 型号"这类上下文彻底打乱。
"""

from __future__ import annotations

import io
import re

from docx import Document
from docx.table import Table
from docx.text.paragraph import Paragraph

from fkh.domain.parsing.common import parser_id
from fkh.domain.parsing.types import (
    BlockKind,
    Locator,
    ParsedBlock,
    ParsedDocument,
    ReviewReason,
    TablePayload,
)

_HEADING_PATTERN = re.compile(r"(\d+)")


def _heading_level(style_name: str) -> int | None:
    normalized = style_name.strip().lower()
    if not normalized.startswith(("heading", "标题", "title")):
        return None
    if normalized in {"title", "标题"}:
        return 1
    match = _HEADING_PATTERN.search(normalized)
    return int(match.group(1)) if match else 1


def _table_payload(table: Table) -> tuple[TablePayload, bool]:
    rows: list[tuple[str, ...]] = []
    merged = False
    for row in table.rows:
        cells: list[str] = []
        seen: set[int] = set()
        for cell in row.cells:
            # 合并单元格在 python-docx 中会对同一底层 tc 重复返回，用 id 去重
            if id(cell._tc) in seen:
                merged = True
                continue
            seen.add(id(cell._tc))
            cells.append(" ".join(cell.text.split()))
        if cells:
            rows.append(tuple(cells))

    if not rows:
        return TablePayload(headers=(), rows=()), merged
    return TablePayload(headers=rows[0], rows=tuple(rows[1:])), merged


def parse_docx(
    content: bytes,
    *,
    source_id: str,
    source_sha256: str,
    language: str = "zh-CN",
) -> ParsedDocument:
    document = Document(io.BytesIO(content))

    blocks: list[ParsedBlock] = []
    reasons: set[ReviewReason] = set()
    block_index = 0
    paragraph_index = 0
    table_index = 0

    for item in document.iter_inner_content():
        if isinstance(item, Paragraph):
            text = " ".join(item.text.split())
            paragraph_index += 1
            if not text:
                continue
            try:
                style_name = item.style.name if item.style is not None else ""
            except (AttributeError, KeyError):
                style_name = ""

            level = _heading_level(style_name)
            if level is not None:
                kind = BlockKind.HEADING
            elif "list" in style_name.lower() or "列表" in style_name:
                kind = BlockKind.LIST
            else:
                kind = BlockKind.PARAGRAPH

            blocks.append(
                ParsedBlock(
                    kind=kind,
                    text=text,
                    level=level,
                    locator=Locator(block_index=block_index, paragraph_index=paragraph_index - 1),
                )
            )
            block_index += 1

        elif isinstance(item, Table):
            payload, merged = _table_payload(item)
            if merged:
                reasons.add(ReviewReason.TABLE_STRUCTURE_UNCERTAIN)
            if not payload.headers:
                reasons.add(ReviewReason.TABLE_STRUCTURE_UNCERTAIN)
            blocks.append(
                ParsedBlock(
                    kind=BlockKind.TABLE,
                    text=payload.to_text(),
                    table=payload,
                    locator=Locator(block_index=block_index, table_index=table_index),
                )
            )
            table_index += 1
            block_index += 1

    if not blocks:
        reasons.add(ReviewReason.EMPTY_DOCUMENT)

    return ParsedDocument(
        source_id=source_id,
        source_sha256=source_sha256,
        mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        parser_id=parser_id("docx", "python-docx"),
        blocks=tuple(blocks),
        language=language,
        review_reasons=tuple(sorted(reasons)),
        metadata={"paragraph_count": paragraph_index, "table_count": table_index},
    )
