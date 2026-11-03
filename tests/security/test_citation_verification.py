"""引用核验与拒答判定的纯逻辑测试。

不需要数据库：这一层的输入是「本次上下文块」+「模型给的 ID 列表」，
输出是符合 AnswerResponse 契约的状态与引用。所有防伪规则都能在这里固定下来。
"""

from __future__ import annotations

import json
import uuid
from dataclasses import FrozenInstanceError
from pathlib import Path
from uuid import UUID

import pytest
from jsonschema import Draft202012Validator

from fkh.retrieval.citation import (
    AnswerStatus,
    Citation,
    CitationBuild,
    RefusalCode,
    build_citations,
    collect_ids,
    decide,
    to_response,
)
from fkh.retrieval.context import ContextBlock

pytestmark = pytest.mark.security

SCHEMA = json.loads(
    (Path(__file__).resolve().parents[2] / "contracts" / "schema" / "answer_response.json").read_text(
        encoding="utf-8"
    )
)


def _block(text: str = "片段", *, version: UUID | None = None) -> ContextBlock:
    return ContextBlock(
        chunk_id=uuid.uuid4(),
        version_id=version or uuid.uuid4(),
        document_id=uuid.uuid4(),
        from_parent=False,
        text=text,
        tokens=3,
        locator={"block_index": 0, "page_start": 1},
        heading_path=("第 1 章",),
    )


def _knowledge_map(blocks: list[ContextBlock]) -> dict[str, str]:
    return {str(block.version_id): str(uuid.uuid4()) for block in blocks}


def test_all_ids_valid_yields_answered() -> None:
    blocks = [_block(), _block()]
    build = build_citations([str(b.chunk_id) for b in blocks], blocks, knowledge_id_by_version=_knowledge_map(blocks))
    plan = decide(blocks, build, answer_text="冷凝管接口为 24/29。")

    assert plan.status is AnswerStatus.ANSWERED
    assert plan.requires_human_review is False
    assert plan.refusal_code is None
    assert len(plan.citations) == 2


def test_fabricated_id_forces_human_review() -> None:
    """模型编造引用时，不能静默丢掉照常作答——必须留痕并转人工。"""
    blocks = [_block()]
    valid = str(blocks[0].chunk_id)
    build = build_citations([valid, str(uuid.uuid4())], blocks, knowledge_id_by_version=_knowledge_map(blocks))

    assert build.unknown_ids
    plan = decide(blocks, build, answer_text="答案")

    assert plan.status is AnswerStatus.HUMAN_REVIEW_REQUIRED
    assert plan.requires_human_review is True
    assert plan.refusal_code is RefusalCode.HUMAN_REVIEW_REQUIRED
    # 有效引用仍然保留，便于人工核对
    assert len(plan.citations) == 1


def test_all_ids_fabricated_refuses() -> None:
    blocks = [_block()]
    build = build_citations(
        [str(uuid.uuid4()), str(uuid.uuid4())], blocks, knowledge_id_by_version=_knowledge_map(blocks)
    )
    plan = decide(blocks, build, answer_text="答案")

    assert plan.status is AnswerStatus.REFUSED
    assert plan.refusal_code is RefusalCode.INSUFFICIENT_EVIDENCE
    assert plan.requires_human_review is True
    assert plan.citations == ()


def test_no_context_refuses_without_review() -> None:
    """没有证据是正常业务结果，不该占用人工复核。"""
    build = CitationBuild(citations=(), unknown_ids=(), duplicates=0)
    plan = decide([], build, answer_text="")

    assert plan.status is AnswerStatus.REFUSED
    assert plan.refusal_code is RefusalCode.NO_EVIDENCE
    assert plan.requires_human_review is False


def test_evidence_exists_but_model_cites_nothing() -> None:
    blocks = [_block()]
    build = build_citations([], blocks, knowledge_id_by_version=_knowledge_map(blocks))
    plan = decide(blocks, build, answer_text="凭印象作答")

    assert plan.status is AnswerStatus.REFUSED
    assert plan.refusal_code is RefusalCode.INSUFFICIENT_EVIDENCE
    assert plan.requires_human_review is True


def test_duplicate_ids_are_deduplicated() -> None:
    blocks = [_block()]
    cid = str(blocks[0].chunk_id)
    build = build_citations([cid, cid, cid], blocks, knowledge_id_by_version=_knowledge_map(blocks))

    assert len(build.citations) == 1
    assert build.duplicates == 2


def test_blank_ids_ignored() -> None:
    blocks = [_block()]
    cid = str(blocks[0].chunk_id)
    build = build_citations(["", "  ", cid], blocks, knowledge_id_by_version=_knowledge_map(blocks))

    assert len(build.citations) == 1
    assert build.unknown_ids == ()


def test_citation_fields_come_from_system_not_model() -> None:
    """version_id 与 locator 必须取系统侧数据，模型无法影响引用定位。"""
    block = _block()
    build = build_citations(
        [str(block.chunk_id)], [block], knowledge_id_by_version=_knowledge_map([block])
    )
    citation = build.citations[0]

    assert citation.version_id == str(block.version_id)
    assert citation.locator == block.locator


def test_empty_answer_text_with_citations_needs_review() -> None:
    blocks = [_block()]
    build = build_citations([str(blocks[0].chunk_id)], blocks, knowledge_id_by_version=_knowledge_map(blocks))
    plan = decide(blocks, build, answer_text="   ")

    assert plan.status is AnswerStatus.REFUSED
    assert plan.requires_human_review is True


def test_response_matches_contract_for_answered() -> None:
    blocks = [_block(), _block()]
    build = build_citations([str(b.chunk_id) for b in blocks], blocks, knowledge_id_by_version=_knowledge_map(blocks))
    plan = decide(blocks, build, answer_text="答案")
    payload = to_response(plan, answer_text="答案", trace_id="t-1", index_generation=3)

    Draft202012Validator(SCHEMA).validate(payload)


def test_response_matches_contract_for_refused() -> None:
    plan = decide([], CitationBuild(citations=(), unknown_ids=(), duplicates=0), answer_text="")
    payload = to_response(plan, answer_text="", trace_id="t-2", index_generation=0)

    Draft202012Validator(SCHEMA).validate(payload)


def test_response_matches_contract_for_human_review() -> None:
    blocks = [_block()]
    build = build_citations(
        [str(blocks[0].chunk_id), str(uuid.uuid4())],
        blocks,
        knowledge_id_by_version=_knowledge_map(blocks),
    )
    plan = decide(blocks, build, answer_text="答案")
    payload = to_response(plan, answer_text="答案", trace_id="t-3", index_generation=1)

    Draft202012Validator(SCHEMA).validate(payload)


def test_answered_without_citation_is_rejected_by_builder() -> None:
    """契约不允许空引用的 ANSWERED，组装层要拦住而不是交给下游校验。"""
    plan = type(decide([], CitationBuild((), (), 0), answer_text=""))(
        status=AnswerStatus.ANSWERED, citations=(), requires_human_review=False, refusal_code=None
    )
    with pytest.raises(ValueError, match="至少有一条引用"):
        to_response(plan, answer_text="x", trace_id="t", index_generation=0)


def test_collect_ids_normalises() -> None:
    assert collect_ids([" a ", "", "a", "b", "  "]) == ("a", "b")


def test_citation_dataclass_is_immutable() -> None:
    citation = Citation(evidence_id="e", knowledge_id="k", version_id="v", locator={})
    with pytest.raises(FrozenInstanceError):
        citation.evidence_id = "other"  # type: ignore[misc]
