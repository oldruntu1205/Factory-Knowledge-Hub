"""回归评测工具的单测：判定顺序（禁止串优先）与价格用例生成。"""

from __future__ import annotations

from fkh.tools.answer_regression import build_price_cases, evaluate_case


def test_forbidden_string_wins_even_if_required_present() -> None:
    payload = {"status": "ANSWERED", "answer": "价格不含税", "citations": [{"evidence_id": "x"}]}
    ok, reasons = evaluate_case(payload, {"status": "ANSWERED", "any": ["含税"], "not": ["不含税"]})
    assert ok is False
    assert any("禁止串" in reason for reason in reasons), "禁止串必须先判，否则会被子串命中骗过"


def test_required_any_and_status() -> None:
    payload = {"status": "ANSWERED", "answer": "标口范围 14-70mm", "citations": [{"evidence_id": "x"}]}
    ok, reasons = evaluate_case(payload, {"status": "ANSWERED", "any": ["14", "70"]})
    assert ok and reasons == []

    ok, reasons = evaluate_case(payload, {"status": "REFUSED"})
    assert ok is False and "status" in reasons[0]


def test_require_citations_flag() -> None:
    ok, reasons = evaluate_case(
        {"status": "ANSWERED", "answer": "答案", "citations": []}, {"require_citations": True}
    )
    assert ok is False and "引用" in reasons[0]


def test_build_price_cases_requires_unique_needle() -> None:
    text = "A112420 200mm 39.00\nA112420 200mm 39.00\nB123456 300mm 8.50\n"
    cases = build_price_cases(text, limit=5)
    codes = {case["id"] for case in cases}
    assert "price-B123456" in codes
    assert "price-A112420" not in codes, "整行在语料里重复时不能当 ground truth"
    assert cases[0]["expect"]["status"] == "ANSWERED"
