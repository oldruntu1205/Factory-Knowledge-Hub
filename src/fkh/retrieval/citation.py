"""引用核验与拒答判定。

问题：生成模型可能编造引用——给出一个看起来合理、实际不存在或不在本次上下文里的
evidence_id。事后去数据库查"这条存不存在"是不够的：一条记录存在，不代表它被提供给了
这次回答。模型应当只能引用**本次实际给它的证据**。

因此规则是白名单式的：

1. 模型只输出 evidence_id，不输出 version_id / locator / 正文；
2. 系统按 ID 在本次上下文块里查找，找不到即判定为编造；
3. 找到的引用，其 version_id 与 locator 一律取系统侧数据，不采信模型给出的任何内容字段；
4. 编造引用不得静默丢弃后照常作答——只要出现，就要求人工复核。

拒答与"编造"分开：
- 没有任何可用证据 → NO_EVIDENCE，这是正常结果，不需要人工；
- 有证据但模型没给出任何有效引用 → INSUFFICIENT_EVIDENCE，需要人工（说明生成异常）。
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from uuid import UUID

from fkh.retrieval.context import ContextBlock


class AnswerStatus(StrEnum):
    ANSWERED = "ANSWERED"
    REFUSED = "REFUSED"
    HUMAN_REVIEW_REQUIRED = "HUMAN_REVIEW_REQUIRED"


class RefusalCode(StrEnum):
    NO_EVIDENCE = "NO_EVIDENCE"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    CONFLICTING_EVIDENCE = "CONFLICTING_EVIDENCE"
    ACCESS_DENIED = "ACCESS_DENIED"
    OUT_OF_SCOPE = "OUT_OF_SCOPE"
    HUMAN_REVIEW_REQUIRED = "HUMAN_REVIEW_REQUIRED"
    SOURCE_REVOKED = "SOURCE_REVOKED"


@dataclass(frozen=True, slots=True)
class Citation:
    evidence_id: str
    knowledge_id: str
    version_id: str
    locator: dict[str, Any]


@dataclass(frozen=True, slots=True)
class CitationBuild:
    citations: tuple[Citation, ...]
    # 模型给出、但不在本次上下文里的 ID。这些是编造或过时引用。
    unknown_ids: tuple[str, ...]
    # 重复给出的 ID 数
    duplicates: int


@dataclass(frozen=True, slots=True)
class AnswerPlan:
    status: AnswerStatus
    citations: tuple[Citation, ...]
    requires_human_review: bool
    refusal_code: RefusalCode | None


def build_citations(
    proposed_ids: Sequence[str],
    blocks: Sequence[ContextBlock],
    *,
    knowledge_id_by_version: dict[str, str],
) -> CitationBuild:
    """把模型给出的 ID 列表转成受控引用。

    version_id 与 locator 取自 blocks（系统侧），模型无法影响引用内容与定位。
    """
    by_id = {str(block.chunk_id): block for block in blocks}
    seen: set[str] = set()
    citations: list[Citation] = []
    unknown: list[str] = []
    duplicates = 0

    for raw in proposed_ids:
        evidence_id = str(raw).strip()
        if not evidence_id:
            continue
        if evidence_id in seen:
            duplicates += 1
            continue
        seen.add(evidence_id)

        block = by_id.get(evidence_id)
        if block is None:
            unknown.append(evidence_id)
            continue

        version_key = str(block.version_id)
        citations.append(
            Citation(
                evidence_id=evidence_id,
                knowledge_id=knowledge_id_by_version.get(version_key, ""),
                version_id=version_key,
                locator=block.locator or {},
            )
        )

    return CitationBuild(citations=tuple(citations), unknown_ids=tuple(unknown), duplicates=duplicates)


def decide(
    blocks: Sequence[ContextBlock],
    build: CitationBuild,
    *,
    answer_text: str,
) -> AnswerPlan:
    """根据上下文与核验结果给出最终状态。

    判定顺序不能颠倒：先看有没有证据，再看引用是否可用，最后才看是否编造。
    反过来会让"无证据"被误报成"模型编造"，排障方向就错了。
    """
    if not blocks:
        return AnswerPlan(
            status=AnswerStatus.REFUSED,
            citations=(),
            requires_human_review=False,
            refusal_code=RefusalCode.NO_EVIDENCE,
        )

    if not build.citations:
        # 有证据却一条都没引用上：要么模型没给，要么全是编造。两种都需要人看。
        return AnswerPlan(
            status=AnswerStatus.REFUSED,
            citations=(),
            requires_human_review=True,
            refusal_code=RefusalCode.INSUFFICIENT_EVIDENCE,
        )

    if build.unknown_ids:
        # 部分引用是编造的：保留可用引用，但整条回答不得直接给用户。
        # 不能"悄悄丢掉伪造项照常返回"——那会让伪造行为不留痕迹。
        return AnswerPlan(
            status=AnswerStatus.HUMAN_REVIEW_REQUIRED,
            citations=build.citations,
            requires_human_review=True,
            refusal_code=RefusalCode.HUMAN_REVIEW_REQUIRED,
        )

    if not answer_text.strip():
        return AnswerPlan(
            status=AnswerStatus.REFUSED,
            citations=build.citations,
            requires_human_review=True,
            refusal_code=RefusalCode.INSUFFICIENT_EVIDENCE,
        )

    return AnswerPlan(
        status=AnswerStatus.ANSWERED,
        citations=build.citations,
        requires_human_review=False,
        refusal_code=None,
    )


def to_response(
    plan: AnswerPlan,
    *,
    answer_text: str,
    trace_id: str,
    index_generation: int,
) -> dict[str, Any]:
    """组装成 AnswerResponse（contracts/schema/answer_response.json）。

    契约要求：REFUSED 必须带 refusal_code；ANSWERED 至少一条 citation。
    """
    if plan.status is AnswerStatus.ANSWERED and not plan.citations:
        raise ValueError("ANSWERED 状态必须至少有一条引用，契约不允许空引用回答")

    payload: dict[str, Any] = {
        "status": plan.status.value,
        "answer": answer_text,
        "requires_human_review": plan.requires_human_review,
        "trace_id": trace_id,
        "index_generation": index_generation,
        "citations": [
            {
                "evidence_id": citation.evidence_id,
                "knowledge_id": citation.knowledge_id,
                "version_id": citation.version_id,
                "locator": citation.locator,
            }
            for citation in plan.citations
        ],
    }
    if plan.refusal_code is not None:
        payload["refusal_code"] = plan.refusal_code.value
    else:
        payload["refusal_code"] = None
    return payload


def collect_ids(evidence_ids: Collection[str]) -> tuple[str, ...]:
    """规范化 ID 集合：去空白、去空串、保序去重。"""
    seen: set[str] = set()
    result: list[str] = []
    for raw in evidence_ids:
        value = str(raw).strip()
        if not value or value in seen:
            continue
        seen.add(value)
        result.append(value)
    return tuple(result)


def version_keys(blocks: Sequence[ContextBlock]) -> set[UUID]:
    return {block.version_id for block in blocks}
