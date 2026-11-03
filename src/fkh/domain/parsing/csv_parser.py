"""CSV / TSV 解析。

工厂的产品参数表常以 CSV 维护，列名与单位是检索的关键：
"24/29" 这样的接口规格脱离列名后无法判断含义，因此表头必须随切片一起保留。
"""

from __future__ import annotations

import csv
import io

from fkh.domain.parsing.types import (
    BlockKind,
    Locator,
    ParsedBlock,
    ParsedDocument,
    ReviewReason,
    TablePayload,
)

# 国内工厂资料常见 GBK 编码；顺序尝试，最后一个用替换模式避免整份文档失败
_ENCODINGS = ("utf-8-sig", "gb18030", "utf-16")

# 表头常见特征词。命中任意一个即认为存在表头：宁可漏判也不误判，
# 把有表头的表当成无表头，会让整份规格表被拖进人工复核队列。
_HEADER_HINTS = (
    "型号", "规格", "名称", "长度", "单位", "接口", "数量", "材质", "批号",
    "参数", "尺寸", "容积", "壁厚", "公差", "编号", "备注",
    "model", "spec", "size", "name", "unit", "qty", "type", "code",
)


def _decode(content: bytes) -> tuple[str, str]:
    for encoding in _ENCODINGS:
        try:
            return content.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    return content.decode("utf-8", errors="replace"), "utf-8/replace"


def _looks_like_data_row(row: tuple[str, ...]) -> bool:
    """判断首行更像数据行而不是表头。

    判据：每个单元格都含数字，且整体不含表头特征词。
    "24/29,300,4.2" 命中；"型号,接口,长度(mm)" 与 "a,b,c" 不命中。
    """
    cells = [cell.strip() for cell in row if cell.strip()]
    if not cells:
        return False
    if not all(any(char.isdigit() for char in cell) for cell in cells):
        return False
    lowered = " ".join(cells).lower()
    return not any(hint in lowered for hint in _HEADER_HINTS)


def parse_csv(
    content: bytes,
    *,
    source_id: str,
    source_sha256: str,
    language: str = "zh-CN",
) -> ParsedDocument:
    text, encoding = _decode(content)
    delimiter = "\t" if text.count("\t") > text.count(",") else ","
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    rows = [tuple(cell.strip() for cell in row) for row in reader if any(cell.strip() for cell in row)]

    reasons: set[ReviewReason] = set()
    warnings: list[str] = [f"encoding={encoding}"]

    if not rows:
        return ParsedDocument(
            source_id=source_id,
            source_sha256=source_sha256,
            mime_type="text/csv",
            parser_id="csv/stdlib",
            blocks=(),
            language=language,
            review_reasons=(ReviewReason.EMPTY_DOCUMENT,),
        )

    header, data = rows[0], rows[1:]
    if _looks_like_data_row(header):
        # 首行全是数字，说明这份表很可能没有表头：无法确定列含义，必须人工确认
        reasons.add(ReviewReason.TABLE_STRUCTURE_UNCERTAIN)
        warnings.append("首行为纯数值，疑似缺少表头")

    width = len(header)
    ragged = [index for index, row in enumerate(data, start=2) if len(row) != width]
    if ragged:
        reasons.add(ReviewReason.TABLE_STRUCTURE_UNCERTAIN)
        warnings.append(f"列数不一致的行：{ragged[:5]}")

    table = TablePayload(headers=header, rows=tuple(data))
    block = ParsedBlock(
        kind=BlockKind.TABLE,
        text=table.to_text(),
        table=table,
        locator=Locator(block_index=0, table_index=0),
    )

    return ParsedDocument(
        source_id=source_id,
        source_sha256=source_sha256,
        mime_type="text/csv",
        parser_id="csv/stdlib",
        blocks=(block,),
        language=language,
        review_reasons=tuple(sorted(reasons)),
        warnings=tuple(warnings),
        metadata={"encoding": encoding, "row_count": len(data), "delimiter": delimiter},
    )
