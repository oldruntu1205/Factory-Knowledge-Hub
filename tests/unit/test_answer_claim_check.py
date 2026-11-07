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


def test_dimension_uses_unit_from_its_own_column_header() -> None:
    """该列表头写了 (mm)，单元格只有 24*24 —— 单位属于这一列，才算支持。"""
    evidence = "货号 上标口*下标口(mm) 单价\nA112420 24*24 39.00"
    report = check_answer("上标口 24×24mm。", [evidence])
    assert report.supported is True


def test_unit_in_unrelated_column_does_not_support() -> None:
    """同一行别的列写着 200mm，不能认定 24*24 的单位是 mm（2026-11-07 复审）。"""
    evidence = "货号 规格 管径*总长 上标口*下标口 单价\nA112420 200mm 38*300 24*24 39.00"
    report = check_answer("上标口 24×24mm。", [evidence])
    assert report.unsupported_numbers == ("24×24mm",)


def test_column_header_with_different_unit_is_unsupported() -> None:
    """该列表头声明 cm，claim 说 mm：单位不一致。"""
    evidence = "货号 上标口*下标口(cm) 单价\nA112420 24*24 39.00"
    report = check_answer("上标口 24×24mm。", [evidence])
    assert report.unsupported_numbers == ("24×24mm",)


def test_mixed_units_without_clear_ownership_is_unsupported() -> None:
    """一行里 mm 与 cm 并存、目标列没有单位声明：归属不明，不支持。"""
    evidence = (
        "货号 管径 上标口*下标口 长度\n"
        "A112420 38mm 24*24 300cm"
    )
    report = check_answer("上标口 24×24mm。", [evidence])
    assert report.unsupported_numbers == ("24×24mm",)


def test_dimension_with_conflicting_unit_is_unsupported() -> None:
    """表头是 mm、claim 说 cm：单位不一致，不能算支持（2026-11-07 终审）。"""
    evidence = "货号 规格 管径*总长 上标口*下标口 单价\nA112420 200mm 38*300 24*24 39.00"
    report = check_answer("上标口 24×24cm。", [evidence])
    assert report.unsupported_numbers == ("24×24cm",)


def test_dimension_without_any_unit_is_unsupported() -> None:
    """单位完全缺失：仅结构相同不足以支持。"""
    report = check_answer("上标口 24×24mm。", ["上标口*下标口 24×24"])
    assert report.unsupported_numbers == ("24×24mm",)


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


def test_reason_codes_name_the_class_of_failure() -> None:
    """裁决要求内部显式记录类别码，而不是只有计数。"""
    report = check_answer("货号 A999999，单价 88.00 元。", ["货号 A112420 单价 39.00 元。"])
    assert report.reason_codes == ("UNSUPPORTED_CODE", "UNSUPPORTED_NUMBER")

    only_code = check_answer("货号 A999999。", ["货号 A112420 单价 39.00 元。"])
    assert only_code.reason_codes == ("UNSUPPORTED_CODE",)


# --- literal-support-v4（2026-11-07 L1 裁决）：单位覆盖缺口 ---------------------
# 背景：v3 的单位白名单只有 11 个，`0.2MPa` / `60秒` / `1.5倍` 不被提取，
# 实测出现过"证据写 0.2MPa、答案编 1.5 倍工作压力，仍判 ANSWERED 并附真实引用"。

EVIDENCE = "气密性测试：将冷凝管两端封堵后充气至 0.2MPa，保压 60 秒，压力下降不超过 5% 为合格。"


def test_fabricated_multiplier_is_extracted_and_unsupported() -> None:
    answer = "测试压力通常为设备工作压力的1.5倍或更高。"
    codes, numbers = extract_claims(answer)
    assert "1.5倍" in numbers
    check = check_answer(answer, [EVIDENCE])
    assert not check.supported
    assert check.unsupported_numbers == ("1.5倍",)


def test_pressure_and_duration_claims_are_supported_by_literal_evidence() -> None:
    answer = "充气至0.2MPa，保压60秒，压力下降不超过5%。"
    check = check_answer(answer, [EVIDENCE])
    assert check.numbers_total == 3
    assert check.supported, check.unsupported_numbers


def test_multiplier_does_not_interchange_with_bare_number_or_percentage() -> None:
    """裁决 §3：倍、裸数、百分比三者不得互认。"""
    assert not check_answer("压力为1.5倍。", ["测试压力为 1.5"]).supported
    assert not check_answer("压力为1.5倍。", ["测试压力为 150%"]).supported
    # 相同数值+相同单位才算支持
    assert check_answer("压力为1.5倍。", ["测试压力为 1.5倍"]).supported


def test_same_value_in_uncited_evidence_does_not_support() -> None:
    check = check_answer("充气至0.2MPa。", ["与本题无关的另一段：某处 0.5MPa"])
    assert not check.supported


def test_overlapping_units_and_boundaries() -> None:
    """MPa 不能被拆成 Pa；单字母单位不能从复合单位里切出来。"""
    codes, numbers = extract_claims("压力0.2MPa，转速3000rpm，功率1.5kW，扭矩5N·m。")
    assert set(numbers) == {"0.2MPa", "3000rpm", "1.5kW", "5N·m"}
    assert check_answer("压力0.2MPa。", ["充气至 0.2MPa"]).supported
    # 证据里只有 kPa，不能支持 MPa
    assert not check_answer("压力0.2MPa。", ["充气至 0.2kPa"]).supported
    # 复合单位不得被拆开：证据写 5Nm，不能支持 5mm
    assert not check_answer("长度5mm。", ["扭矩 5Nm"]).supported


def test_model_codes_are_not_confused_with_units() -> None:
    codes, numbers = extract_claims("A112420 规格 200mm。")
    assert codes == ["A112420"]
    assert numbers == ["200mm"]
