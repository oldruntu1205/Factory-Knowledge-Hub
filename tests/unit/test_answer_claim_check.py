"""一致性检查：区间/尺寸整体核验、边界、单位白名单、不适用。"""

from __future__ import annotations

from fkh.answer.consistency import check_answer, extract_claims, normalize


def test_range_must_match_as_whole() -> None:
    """14 与 70 分散在两处，不能拼成 14-70mm 的支持。"""
    evidence = "标口 14 号；长度 70 号。"
    report = check_answer("标口范围是 14-70mm。", [evidence])
    assert report.unsupported_numbers == ("14-70mm",)


def test_range_supported_when_evidence_has_same_range() -> None:
    report = check_answer("标口范围 14-70mm。", ["冷凝管的标口从14-70mm，长度可定制。"])
    assert report.supported is True


def test_number_boundary_14_is_not_supported_by_114() -> None:
    report = check_answer("标口 14mm。", ["标口 114mm。"])
    assert report.unsupported_numbers == ("14mm",)


def test_same_number_different_unit_is_unsupported() -> None:
    report = check_answer("长度 14cm。", ["长度 14mm。"])
    assert report.unsupported_numbers == ("14cm",)


def test_dimension_without_unit_in_evidence_is_supported() -> None:
    """目录表把单位写在表头，单元格只有 24×24。"""
    report = check_answer("上标口 24×24mm。", ["上标口*下标口 24×24"])
    assert report.supported is True


def test_dimension_structural_change_is_unsupported() -> None:
    report = check_answer("上标口 24×25mm。", ["上标口*下标口 24×24"])
    assert report.unsupported_numbers == ("24×25mm",)


def test_short_code_is_not_supported_by_longer_code() -> None:
    report = check_answer("货号 A11242。", ["货号 A112421 单价 39.00 元。"])
    assert report.unsupported_codes == ("A11242",)


def test_fullwidth_digits_are_normalized() -> None:
    report = check_answer("标口 １４－７０ｍｍ。", ["标口 14-70mm。"])
    assert report.supported is True


def test_no_claims_means_not_applicable() -> None:
    report = check_answer("价格不含税。", ["目录只列了单价。"])
    assert report.applicable is False
    assert normalize("Ａ１") == "A1"


def test_price_requires_unit_to_match() -> None:
    report = check_answer("单价 39.00 元。", ["单价 39.00"])
    assert report.unsupported_numbers == ("39.00元",)


def test_extract_claims_keeps_range_and_dimension_structures() -> None:
    codes, numbers = extract_claims("标口 14-70mm，上标口 24×24mm，单价 39.00 元，货号 A112420。")
    assert codes == ["A112420"]
    assert numbers == ["14-70mm", "24×24mm", "39.00元"]
