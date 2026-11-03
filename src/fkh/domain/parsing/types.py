"""解析结果与定位信息的数据模型。

定位（Locator）是这一层的核心产出。L1 §4.1 要求保留页码、标题路径、表格列名、
产品型号、单位与原文坐标/段落锚点，目的是：任何一条回答都能回到原文的确切位置，
而不是"在某份文档里"。没有可靠定位的证据，在对外客服场景等于不可审计。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class BlockKind(StrEnum):
    HEADING = "heading"
    PARAGRAPH = "paragraph"
    LIST = "list"
    TABLE = "table"
    CAPTION = "caption"
    CODE = "code"


class ReviewReason(StrEnum):
    """必须进入人工复核队列的原因。"""

    LOW_OCR_CONFIDENCE = "LOW_OCR_CONFIDENCE"
    CROSS_PAGE_TABLE = "CROSS_PAGE_TABLE"
    MISSING_UNIT = "MISSING_UNIT"
    TABLE_STRUCTURE_UNCERTAIN = "TABLE_STRUCTURE_UNCERTAIN"
    EMPTY_DOCUMENT = "EMPTY_DOCUMENT"
    HEADING_HEURISTIC = "HEADING_HEURISTIC"
    PARSER_FALLBACK = "PARSER_FALLBACK"


@dataclass(frozen=True, slots=True)
class Locator:
    """原文定位。

    字段允许为 None，但 None 的含义是"该维度不适用"，不是"不限制"。
    入库时不得把 locator 为空的切片当作可对外引用的证据。
    """

    block_index: int = 0
    page_start: int | None = None
    page_end: int | None = None
    paragraph_index: int | None = None
    table_index: int | None = None
    row_index: int | None = None
    bbox: tuple[float, float, float, float] | None = None
    anchor: str | None = None

    def to_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "block_index": self.block_index,
            "page_start": self.page_start,
            "page_end": self.page_end,
            "paragraph_index": self.paragraph_index,
            "table_index": self.table_index,
            "row_index": self.row_index,
            "bbox": list(self.bbox) if self.bbox is not None else None,
            "anchor": self.anchor,
        }
        return payload


@dataclass(frozen=True, slots=True)
class TablePayload:
    """表格结构。headers 与 rows 保持原文列序，不做列重排。"""

    headers: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]
    caption: str | None = None
    unit_hint: str | None = None

    def to_text(self) -> str:
        """渲染为可检索文本。

        表头与单位在首行给出；按行切片时由 iter_row_texts 逐行重复表头，
        否则单独一行数据（如 "24/29 | 300 | 4.2"）在检索时无法判断列含义。
        """
        lines: list[str] = []
        if self.caption:
            lines.append(self.caption)
        header = " | ".join(self.headers)
        if self.unit_hint:
            header = f"{header}（单位：{self.unit_hint}）"
        if header.strip(" |"):
            lines.append(header)
        lines.extend(" | ".join(row) for row in self.rows)
        return "\n".join(lines)

    def iter_row_texts(self) -> list[str]:
        """每行都带上表头与单位，用于超长表格按行切分。"""
        header = " | ".join(self.headers)
        if self.unit_hint:
            header = f"{header}（单位：{self.unit_hint}）"
        prefix = f"{self.caption}\n{header}\n" if self.caption else f"{header}\n"
        return [prefix + " | ".join(row) for row in self.rows]


@dataclass(frozen=True, slots=True)
class ParsedBlock:
    kind: BlockKind
    text: str
    locator: Locator
    level: int | None = None
    table: TablePayload | None = None
    ocr_confidence: float | None = None


@dataclass(frozen=True, slots=True)
class ParsedDocument:
    source_id: str
    source_sha256: str
    mime_type: str
    parser_id: str
    blocks: tuple[ParsedBlock, ...]
    page_count: int | None = None
    language: str = "zh-CN"
    review_reasons: tuple[ReviewReason, ...] = ()
    warnings: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def needs_human_review(self) -> bool:
        return bool(self.review_reasons)

    @property
    def text_length(self) -> int:
        return sum(len(block.text) for block in self.blocks)


class ParseError(ValueError):
    """解析失败。调用方必须把该文档标记为不可入库，而不是存一个空壳。"""
