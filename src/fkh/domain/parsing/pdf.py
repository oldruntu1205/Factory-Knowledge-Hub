"""PDF 解析。

工厂的产品目录、图纸说明、检验报告大量是 PDF，且以扫描件居多。
本解析器只处理**可提取文本**的 PDF；扫描件需要 OCR 通道，而 OCR 引擎与语言
尚未由 ADR-05 确定，因此这里不猜默认引擎，只把页面标记为需要复核。

选型说明：使用 pdfplumber（MIT）而非 PyMuPDF。PyMuPDF 为 AGPL-3.0，
闭源商业交付下会带来传染性许可风险，而本项目需要保留原文坐标与表格结构，
pdfplumber 在 MIT 许可下能提供这两项能力。
"""

from __future__ import annotations

import io
import statistics
from dataclasses import dataclass
from typing import cast

import pdfplumber

from fkh.domain.parsing.common import parser_id
from fkh.domain.parsing.types import (
    BlockKind,
    Locator,
    ParsedBlock,
    ParsedDocument,
    ReviewReason,
    TablePayload,
)

BBox = tuple[float, float, float, float]


@dataclass(frozen=True, slots=True)
class _LineEntry:
    top: float
    text: str
    bbox: BBox
    size: float


@dataclass(frozen=True, slots=True)
class _TableEntry:
    top: float
    payload: TablePayload
    bbox: BBox


_Entry = _LineEntry | _TableEntry


def _inside(bbox: BBox, boxes: list[BBox]) -> bool:
    centre_x = (bbox[0] + bbox[2]) / 2
    centre_y = (bbox[1] + bbox[3]) / 2
    return any(box[0] <= centre_x <= box[2] and box[1] <= centre_y <= box[3] for box in boxes)


def _mode_size(chars: list[dict[str, object]]) -> float:
    sizes: list[float] = []
    for char in chars:
        size = char.get("size")
        if isinstance(size, int | float):
            sizes.append(round(float(size) * 2) / 2)
    if not sizes:
        return 0.0
    return statistics.mode(sizes)


def _table_payload(raw: list[list[str | None]] | None) -> tuple[TablePayload, bool]:
    if not raw:
        return TablePayload(headers=(), rows=()), False

    cleaned: list[tuple[str, ...]] = []
    for row in raw:
        cells = tuple(" ".join((cell or "").split()) for cell in row)
        if any(cells):
            cleaned.append(cells)

    if not cleaned:
        return TablePayload(headers=(), rows=()), False

    header = cleaned[0]
    rows = tuple(cleaned[1:])
    uncertain = len(rows) == 0 or len(header) < 2
    return TablePayload(headers=header, rows=rows), uncertain


def parse_pdf(
    content: bytes,
    *,
    source_id: str,
    source_sha256: str,
    language: str = "zh-CN",
) -> ParsedDocument:
    reasons: set[ReviewReason] = set()
    warnings: list[str] = []

    page_entries: list[list[_Entry]] = []
    all_sizes: list[float] = []
    page_count = 0
    textless_pages: list[int] = []
    previous_table_header: tuple[str, ...] | None = None

    with pdfplumber.open(io.BytesIO(content)) as pdf:
        page_count = len(pdf.pages)

        for page_index, page in enumerate(pdf.pages, start=1):
            found_tables = page.find_tables()
            table_boxes = [cast(BBox, tuple(table.bbox)) for table in found_tables]

            entries: list[_Entry] = []

            for table in found_tables:
                payload, uncertain = _table_payload(table.extract())
                if uncertain:
                    reasons.add(ReviewReason.TABLE_STRUCTURE_UNCERTAIN)
                if previous_table_header is not None and payload.headers == previous_table_header:
                    # 表头与上一页末表相同，通常是同一张表被分页切断
                    reasons.add(ReviewReason.CROSS_PAGE_TABLE)
                if payload.headers:
                    previous_table_header = payload.headers
                entries.append(_TableEntry(float(table.bbox[1]), payload, cast(BBox, tuple(table.bbox))))

            line_count = 0
            for line in page.extract_text_lines():
                # extract_text_lines 返回 x0/top/x1/bottom，没有 bbox 键
                bbox: BBox = (
                    float(line["x0"]),
                    float(line["top"]),
                    float(line["x1"]),
                    float(line["bottom"]),
                )
                if _inside(bbox, table_boxes):
                    continue
                text = str(line.get("text", "")).strip()
                if not text:
                    continue
                size = _mode_size(list(line.get("chars", [])))
                all_sizes.append(size)
                entries.append(_LineEntry(bbox[1], text, bbox, size))
                line_count += 1

            if line_count == 0 and not found_tables:
                textless_pages.append(page_index)

            entries.sort(key=lambda entry: entry.top)
            page_entries.append(entries)

    if textless_pages:
        reasons.add(ReviewReason.PARSER_FALLBACK)
        warnings.append(
            f"第 {textless_pages[:5]} 页无可提取文本，疑似扫描件；需 OCR 通道（ADR-05 未签署，未启用）"
        )

    body_size = statistics.median(all_sizes) if all_sizes else 0.0

    blocks: list[ParsedBlock] = []
    block_index = 0
    heading_detected = False

    for page_index, entries in enumerate(page_entries, start=1):
        for entry in entries:
            if isinstance(entry, _TableEntry):
                blocks.append(
                    ParsedBlock(
                        kind=BlockKind.TABLE,
                        text=entry.payload.to_text(),
                        table=entry.payload,
                        locator=Locator(
                            block_index=block_index,
                            page_start=page_index,
                            page_end=page_index,
                            table_index=block_index,
                            bbox=entry.bbox,
                        ),
                    )
                )
            else:
                level: int | None = None
                if body_size > 0 and entry.size >= body_size * 1.15:
                    level = 1 if entry.size >= body_size * 1.3 else 2
                    heading_detected = True
                blocks.append(
                    ParsedBlock(
                        kind=BlockKind.HEADING if level is not None else BlockKind.PARAGRAPH,
                        text=entry.text,
                        level=level,
                        locator=Locator(
                            block_index=block_index,
                            page_start=page_index,
                            page_end=page_index,
                            bbox=entry.bbox,
                        ),
                    )
                )
            block_index += 1

    if heading_detected:
        # 标题靠字号推断，不是原文显式结构，必须允许人工纠正
        reasons.add(ReviewReason.HEADING_HEURISTIC)

    if not blocks:
        reasons.add(ReviewReason.EMPTY_DOCUMENT)

    return ParsedDocument(
        source_id=source_id,
        source_sha256=source_sha256,
        mime_type="application/pdf",
        parser_id=parser_id("pdf", "pdfplumber"),
        blocks=tuple(blocks),
        page_count=page_count,
        language=language,
        review_reasons=tuple(sorted(reasons)),
        warnings=tuple(warnings),
        metadata={"body_font_size": body_size, "textless_pages": textless_pages},
    )
