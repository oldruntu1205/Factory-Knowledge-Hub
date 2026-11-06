"""目录行抽取的单元测试（不依赖数据库）。"""

from __future__ import annotations

from decimal import Decimal

from fkh.domain.catalog import is_range_price, parse_catalog_text

TEXT = """货号 规格 管径*总长 上标口*下标口 单价
A112420 200mm 38*300 24*24 39.00
A112430 300mm 38*400 24*24 48.00
C190010 10ml白 29*96 288 10/19 19.00
A999999 区间价示例 12.00-15.00
A888888 缺价格示例
说明文字：以上价格含税，有效期至 2023-12-31。
B1234567 长货号示例 8.50
"""


def test_parses_single_price_rows() -> None:
    parsed = parse_catalog_text(TEXT)
    codes = [row.code for row in parsed.rows]
    assert codes == ["A112420", "A112430", "C190010", "B1234567"]
    assert parsed.rows[0].spec == "200mm 38*300 24*24"
    assert parsed.rows[0].price_text == "39.00"
    assert parsed.rows[0].price_value == Decimal("39.00")
    assert parsed.rows[0].raw_text == "A112420 200mm 38*300 24*24 39.00"


def test_range_price_is_not_guessed() -> None:
    parsed = parse_catalog_text(TEXT)
    assert "A999999 区间价示例 12.00-15.00" in parsed.unsupported
    assert all(row.code != "A999999" for row in parsed.rows)
    assert is_range_price("12.00-15.00")


def test_missing_price_is_not_guessed() -> None:
    parsed = parse_catalog_text(TEXT)
    assert "A888888 缺价格示例" in parsed.unsupported


def test_header_and_prose_are_ignored() -> None:
    parsed = parse_catalog_text(TEXT)
    joined = "\n".join(parsed.unsupported)
    # 表头与说明文字不是"像货号但价格不对"的行，应当被直接忽略（不进 unsupported）
    assert "货号 规格" not in joined
    assert "有效期至" not in joined, "普通说明行不应被当成可疑行"
    assert all("有效期" not in row.raw_text for row in parsed.rows)


def test_duplicate_code_keeps_first_and_flags_second() -> None:
    text = "A112420 200mm 39.00\nA112420 300mm 48.00\n"
    parsed = parse_catalog_text(text)
    assert len(parsed.rows) == 1
    assert parsed.rows[0].price_text == "39.00"
    assert "A112420 300mm 48.00" in parsed.unsupported


def test_empty_text_yields_nothing() -> None:
    parsed = parse_catalog_text("")
    assert parsed.rows == ()
    assert parsed.unsupported_count == 0
