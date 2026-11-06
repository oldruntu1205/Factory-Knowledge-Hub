"""证据表格改写：列名与值必须显式配对，认不出就原样返回。"""

from __future__ import annotations

from fkh.answer.tables import has_table, render_evidence

PRICE_TABLE = """货号 规格 管径*总长 上标口*下标口 单价
A112420 200mm 38*300 24*24 39.00
A112430 300mm 38*400 24*24 48.00"""

TUBE_TABLE = """高硼硅试管
货号 直径*长度 壁厚
C190010 10ml 1.2
C190020 20ml 1.4"""


def test_price_table_rows_get_column_names() -> None:
    rendered = render_evidence(PRICE_TABLE)
    assert "表头：货号 | 规格 | 管径*总长 | 上标口*下标口 | 单价" in rendered
    assert "A112420：规格=200mm，管径*总长=38*300，上标口*下标口=24*24，单价=39.00" in rendered


def test_tube_table_rows_get_column_names() -> None:
    rendered = render_evidence(TUBE_TABLE)
    assert "C190010：直径*长度=10ml，壁厚=1.2" in rendered
    assert "高硼硅试管" in rendered, "非表行原样保留"


def test_plain_text_is_untouched() -> None:
    text = "冷凝管的标口从14-70mm，长度均可接受定制。\n价格含税。"
    assert render_evidence(text) == text
    assert has_table(text) is False


def test_row_without_code_is_untouched() -> None:
    text = "货号 规格 单价\n说明：以下为定制规格 100.00"
    rendered = render_evidence(text)
    assert "说明：以下为定制规格 100.00" in rendered
    assert "规格=" not in rendered


def test_column_mismatch_falls_back_to_positional_names() -> None:
    text = "货号 规格 单价\nA112420 200mm 39.00 备注X"
    rendered = render_evidence(text)
    assert "列4=备注X" in rendered


def test_has_table_detects_only_real_tables() -> None:
    assert has_table(PRICE_TABLE) is True
    assert has_table("货号 规格 单价") is False, "只有表头没有数据行不算表"
