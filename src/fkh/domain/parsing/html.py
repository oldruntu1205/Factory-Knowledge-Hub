"""HTML 解析。

工业场景里 HTML 主要来自两个渠道：客户官网产品页、以及办公系统导出的资料。
脚本与样式必须剔除，表格合并单元格必须显式标记——合并单元格会让列与列错位，
错位后的规格表比没有表更危险。
"""

from __future__ import annotations

from bs4 import BeautifulSoup
from bs4.element import Tag

from fkh.domain.parsing.common import parser_id
from fkh.domain.parsing.types import (
    BlockKind,
    Locator,
    ParsedBlock,
    ParsedDocument,
    ReviewReason,
    TablePayload,
)


def _clean(text: str) -> str:
    return " ".join(text.split())


def _table_from_tag(tag: Tag) -> tuple[TablePayload, bool]:
    headers: list[str] = []
    rows: list[tuple[str, ...]] = []
    has_merged = False

    for row_index, tr in enumerate(tag.find_all("tr")):
        cells = tr.find_all(["th", "td"])
        values: list[str] = []
        for cell in cells:
            if cell.has_attr("colspan") or cell.has_attr("rowspan"):
                colspan = int(str(cell.get("colspan", "1")))
                rowspan = int(str(cell.get("rowspan", "1")))
                if colspan > 1 or rowspan > 1:
                    has_merged = True
            values.append(_clean(cell.get_text(" ", strip=True)))
        if not values:
            continue
        if row_index == 0 and any(cell.name == "th" for cell in cells):
            headers = values
        else:
            rows.append(tuple(values))

    if not headers and rows:
        headers = list(rows[0])
        rows = rows[1:]

    return TablePayload(headers=tuple(headers), rows=tuple(rows)), has_merged


def parse_html(
    content: bytes,
    *,
    source_id: str,
    source_sha256: str,
    language: str = "zh-CN",
) -> ParsedDocument:
    soup = BeautifulSoup(content, "lxml")
    for tag in soup(["script", "style", "noscript", "iframe"]):
        tag.decompose()

    blocks: list[ParsedBlock] = []
    reasons: set[ReviewReason] = set()
    block_index = 0

    for element in soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6", "p", "ul", "ol", "table"]):
        if element.name != "table" and element.find_parent("table") is not None:
            continue

        if element.name.startswith("h"):
            text = _clean(element.get_text(" ", strip=True))
            if not text:
                continue
            blocks.append(
                ParsedBlock(
                    kind=BlockKind.HEADING,
                    text=text,
                    level=int(element.name[1]),
                    locator=Locator(block_index=block_index),
                )
            )

        elif element.name == "p":
            text = _clean(element.get_text(" ", strip=True))
            if not text:
                continue
            blocks.append(
                ParsedBlock(
                    kind=BlockKind.PARAGRAPH,
                    text=text,
                    locator=Locator(block_index=block_index),
                )
            )

        elif element.name in {"ul", "ol"}:
            items = [
                _clean(item.get_text(" ", strip=True))
                for item in element.find_all("li", recursive=False)
                if _clean(item.get_text(" ", strip=True))
            ]
            if not items:
                continue
            blocks.append(
                ParsedBlock(
                    kind=BlockKind.LIST,
                    text="\n".join(f"- {item}" for item in items),
                    locator=Locator(block_index=block_index),
                )
            )

        else:  # table
            table, merged = _table_from_tag(element)
            if not table.headers or not table.rows:
                reasons.add(ReviewReason.TABLE_STRUCTURE_UNCERTAIN)
            if merged:
                reasons.add(ReviewReason.TABLE_STRUCTURE_UNCERTAIN)
            blocks.append(
                ParsedBlock(
                    kind=BlockKind.TABLE,
                    text=table.to_text(),
                    table=table,
                    locator=Locator(block_index=block_index, table_index=block_index),
                )
            )

        block_index += 1

    if not blocks:
        reasons.add(ReviewReason.EMPTY_DOCUMENT)

    return ParsedDocument(
        source_id=source_id,
        source_sha256=source_sha256,
        mime_type="text/html",
        parser_id=parser_id("html", "beautifulsoup4"),
        blocks=tuple(blocks),
        language=language,
        review_reasons=tuple(sorted(reasons)),
    )
