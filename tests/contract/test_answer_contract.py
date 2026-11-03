"""契约资产自检：Schema 合法性、正反例、与实现模型的一致性。

这一层的价值在于：契约文件不是文档装饰，而是可执行断言。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from fkh.api.models import AnswerRequest

pytestmark = [pytest.mark.contract]

SCHEMA_DIR = Path(__file__).resolve().parents[2] / "contracts" / "schema"


def _load(name: str) -> dict[str, Any]:
    return json.loads((SCHEMA_DIR / name).read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    "name",
    ["answer_request.json", "answer_response.json", "error.json", "event_envelope.json"],
)
def test_schema_is_valid_draft_2020_12(name: str) -> None:
    Draft202012Validator.check_schema(_load(name))


def test_answer_request_accepts_minimal_payload() -> None:
    validator = Draft202012Validator(_load("answer_request.json"))
    validator.validate(
        {
            "query": "冷凝管常见接口规格有哪些？",
            "knowledge_scope": "internal_product",
            "language": "zh-CN",
        }
    )


@pytest.mark.parametrize(
    "payload",
    [
        {"query": "", "knowledge_scope": "internal_product", "language": "zh-CN"},
        {"query": "x", "knowledge_scope": "c3_secret", "language": "zh-CN"},
        {"query": "x", "knowledge_scope": "internal_product", "language": "ja-JP"},
        {"query": "x", "knowledge_scope": "internal_product", "language": "zh-CN", "role": "admin"},
        {"query": "x", "knowledge_scope": "internal_product", "language": "zh-CN", "max_evidence": 99},
    ],
)
def test_answer_request_rejects_invalid_payload(payload: dict[str, Any]) -> None:
    validator = Draft202012Validator(_load("answer_request.json"))
    assert not validator.is_valid(payload)


def test_answered_without_citation_is_invalid() -> None:
    validator = Draft202012Validator(_load("answer_response.json"))
    assert not validator.is_valid(
        {
            "status": "ANSWERED",
            "answer": "可以定制。",
            "citations": [],
            "requires_human_review": False,
            "trace_id": "t-1",
            "index_generation": 3,
        }
    )


def test_refused_without_code_is_invalid() -> None:
    validator = Draft202012Validator(_load("answer_response.json"))
    assert not validator.is_valid(
        {
            "status": "REFUSED",
            "answer": "无法回答。",
            "citations": [],
            "requires_human_review": False,
            "trace_id": "t-2",
            "index_generation": 3,
        }
    )


def test_refused_with_code_is_valid() -> None:
    validator = Draft202012Validator(_load("answer_response.json"))
    validator.validate(
        {
            "status": "REFUSED",
            "answer": "现有资料不足以回答该问题。",
            "refusal_code": "NO_EVIDENCE",
            "citations": [],
            "requires_human_review": False,
            "trace_id": "t-3",
            "index_generation": 3,
        }
    )


def test_human_review_requires_flag() -> None:
    validator = Draft202012Validator(_load("answer_response.json"))
    assert not validator.is_valid(
        {
            "status": "HUMAN_REVIEW_REQUIRED",
            "answer": "价格需人工确认。",
            "refusal_code": "HUMAN_REVIEW_REQUIRED",
            "citations": [],
            "requires_human_review": False,
            "trace_id": "t-4",
            "index_generation": 3,
        }
    )


def test_event_envelope_rejects_unknown_event_type() -> None:
    validator = Draft202012Validator(_load("event_envelope.json"))
    assert not validator.is_valid(
        {
            "schema_version": "1.0",
            "event_id": "c1f96ec6-6ad6-4e7c-a67c-ef9dd0d33a11",
            "event_type": "knowledge.something.v1",
            "tenant_id": "58e919c9-5d38-4f71-8ad4-18f9f57f8c10",
            "aggregate_id": "0f7c1f5e-c62c-4d4f-920d-5fdfd2e7b1d0",
            "aggregate_version": 1,
            "occurred_at": "2026-10-30T12:00:00Z",
            "trace_id": "trace-1",
            "producer": "review-service",
            "payload": {},
        }
    )


def test_pydantic_model_matches_contract_constraints() -> None:
    """实现模型与契约文件必须同源同义，防止两套定义漂移。"""
    contract = _load("answer_request.json")
    generated = AnswerRequest.model_json_schema()

    assert set(contract["required"]) == set(generated["required"])
    assert set(contract["properties"]) == set(generated["properties"])
    assert contract["additionalProperties"] is False
    assert generated.get("additionalProperties") is False

    assert contract["properties"]["query"]["maxLength"] == generated["properties"]["query"]["maxLength"]
    assert (
        contract["properties"]["knowledge_scope"]["enum"]
        == generated["properties"]["knowledge_scope"]["enum"]
    )
    assert contract["properties"]["max_evidence"]["default"] == generated["properties"]["max_evidence"]["default"]
