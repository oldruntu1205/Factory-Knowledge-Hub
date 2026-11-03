"""Markdown 解析。

Markdown 是编辑端（Obsidian + Git）的载体，因此这条解析路径的保真度直接影响
知识草稿能否无损进入索引。保留标题层级、列表项、表格列名与代码块。
"""

from __future__ import annotations

from markdown_it import MarkdownIt
from markdown_it.token import Token

from fkh.domain.parsing.common import parser_id
from fkh.domain.parsing.types import (
    BlockKind,
    Locator,
    ParsedBlock,
    ParsedDocument,
    ReviewReason,
    TablePayload,
)


def _table_from_tokens(tokens: list[Token], start: int) -> tuple[TablePayload, int]:
    headers: list[str] = []
    rows: list[tuple[str, ...]] = []
    current: list[str] | None = None
    in_thead = False

    index = start + 1
    while index < len(tokens) and tokens[index].type != "table_close":
        token = tokens[index]
        if token.type == "thead_open":
            in_thead = True
        elif token.type == "thead_close":
            in_thead = False
        elif token.type == "tr_open":
            current = []
        elif token.type == "tr_close":
            if current is not None:
                if in_thead:
                    if not headers:
                        headers = current
                else:
                    rows.append(tuple(current))
            current = None
        elif token.type == "inline" and current is not None:
            current.append(token.content.strip())
        index += 1

    return TablePayload(headers=tuple(headers), rows=tuple(rows)), index


def _list_items(tokens: list[Token], start: int) -> tuple[list[str], int]:
    open_type = tokens[start].type
    close_type = open_type.rsplit("_", 1)[0] + "_close"
    depth = 0
    items: list[str] = []
    index = start

    while index < len(tokens):
        token = tokens[index]
        if token.type == open_type:
            depth += 1
        elif token.type == close_type:
            depth -= 1
            if depth == 0:
                return items, index
        elif token.type == "inline" and token.content.strip():
            items.append(token.content.strip())
        index += 1

    return items, index


def parse_markdown(
    content: bytes,
    *,
    source_id: str,
    source_sha256: str,
    language: str = "zh-CN",
) -> ParsedDocument:
    text = content.decode("utf-8-sig")
    # html=False：不允许内联 HTML 穿透，避免把脚本片段带进知识正文
    parser = MarkdownIt("commonmark", {"html": False}).enable("table")
    tokens = parser.parse(text)

    blocks: list[ParsedBlock] = []
    reasons: set[ReviewReason] = set()
    block_index = 0
    index = 0

    while index < len(tokens):
        token = tokens[index]

        if token.type == "heading_open":
            inline = tokens[index + 1] if index + 1 < len(tokens) else None
            heading_text = inline.content.strip() if inline is not None else ""
            if heading_text:
                blocks.append(
                    ParsedBlock(
                        kind=BlockKind.HEADING,
                        text=heading_text,
                        level=int(token.tag[1]) if token.tag.startswith("h") else 1,
                        locator=Locator(block_index=block_index),
                    )
                )
                block_index += 1
            index += 3

        elif token.type == "paragraph_open":
            inline = tokens[index + 1] if index + 1 < len(tokens) else None
            paragraph = inline.content.strip() if inline is not None else ""
            if paragraph:
                blocks.append(
                    ParsedBlock(
                        kind=BlockKind.PARAGRAPH,
                        text=paragraph,
                        locator=Locator(block_index=block_index),
                    )
                )
                block_index += 1
            index += 3

        elif token.type in {"bullet_list_open", "ordered_list_open"}:
            items, close_index = _list_items(tokens, index)
            if items:
                blocks.append(
                    ParsedBlock(
                        kind=BlockKind.LIST,
                        text="\n".join(f"- {item}" for item in items),
                        locator=Locator(block_index=block_index),
                    )
                )
                block_index += 1
            index = close_index + 1

        elif token.type == "fence":
            if token.content.strip():
                blocks.append(
                    ParsedBlock(
                        kind=BlockKind.CODE,
                        text=token.content.rstrip(),
                        locator=Locator(block_index=block_index),
                    )
                )
                block_index += 1
            index += 1

        elif token.type == "table_open":
            table, close_index = _table_from_tokens(tokens, index)
            if not table.headers:
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
            index = close_index + 1

        else:
            index += 1

    if not blocks:
        reasons.add(ReviewReason.EMPTY_DOCUMENT)

    return ParsedDocument(
        source_id=source_id,
        source_sha256=source_sha256,
        mime_type="text/markdown",
        parser_id=parser_id("markdown", "markdown-it-py"),
        blocks=tuple(blocks),
        language=language,
        review_reasons=tuple(sorted(reasons)),
    )
