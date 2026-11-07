"""答案闸门回归：用**脚本化模型**固定"什么结果必须是什么状态"。

为什么要有这一层
----------------
改提示词、换模型、调单位白名单、动引用解析，都会悄悄改变闸门行为。用真实模型做断言
不稳定（同一问题两次结果可能不同），所以这里把模型换成脚本：给定固定的答案文本，
断言系统最终判定的状态与原因码。

它锁的是**闸门**（引用白名单 → 决定状态 → 一致性检查），不是模型质量。
真实模型的评测仍走 `python -m fkh.tools.answer_regression`。

历史回归：
- 2026-11-07 v3 单位白名单漏 MPa/秒/倍，导致"证据写 0.2MPa、答案编 1.5 倍工作压力"
  被判 ANSWERED 并附真实引用 → 本文件 case_fabricated_multiplier 就是那条的固化。
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import psycopg
import pytest
from tests.conftest import create_principal, create_tenant

from fkh.answer.service import AnswerService
from fkh.authz.policy import AclPolicy, Subject
from fkh.domain.review import ReviewService
from fkh.retrieval.citation import AnswerStatus, RefusalCode
from fkh.retrieval.search import SearchContext
from fkh.workers.ingestion import IngestionRequest, ingest_document

pytestmark = pytest.mark.requires_pg

QUALITY_TEXT = """质量检验说明

气密性测试：将冷凝管两端封堵后充气至 0.2MPa，保压 60 秒，压力下降不超过 5% 为合格。

外观检查：观察管身是否有气泡、裂纹、明显划痕，标口端面不得崩边。
"""

GUIDE_TEXT = """冷凝管使用说明（客户版）

售后：自签收之日起 12 个月内，非人为损坏可免费更换；人为损坏提供有偿维修。

清洁保养：使用后用中性洗涤剂清洗，纯水冲洗，自然晾干。
"""


@dataclass
class Scripted:
    """把模型的输出固定住：闸门行为可回归，不受温度与采样影响。"""

    text: str
    model_id: str = "scripted"

    def generate(self, prompt: str, *, max_tokens: int = 512, temperature: float = 0.0) -> Any:
        @dataclass
        class Generated:
            text: str
            model_id: str

        return Generated(text=self.text, model_id=self.model_id)


@pytest.fixture(scope="module")
def corpus(pg_url: str) -> Iterator[dict[str, Any]]:
    from fkh.config import Settings  # noqa: F401  （保持导入路径与生产一致）

    with psycopg.connect(pg_url, autocommit=True) as admin:
        tenant = create_tenant(admin, f"闸门回归 {uuid.uuid4().hex[:6]}")
        principal = create_principal(admin, tenant, f"gate-{uuid.uuid4().hex[:6]}@example.test")
        public_approver = create_principal(
            admin, tenant, f"gate-pub-{uuid.uuid4().hex[:6]}@example.test"
        )

    with psycopg.connect(pg_url) as conn:
        conn.execute("SELECT set_config('app.tenant_id', %s, false)", (tenant,))
        service = ReviewService(conn, uuid.UUID(tenant))
        acl = AclPolicy.from_json({"allow": {"roles": ["staff"]}, "deny": {}})

        quality = ingest_document(
            conn,
            uuid.UUID(tenant),
            IngestionRequest(
                source_id="gate-quality",
                filename="quality.md",
                content=QUALITY_TEXT.encode(),
                knowledge_key="gate-quality",
                title="质量检验说明",
                domain="quality",
                owner_department="qc",
                classification=2,
                audience="internal",
                required_scope="internal_product",
                acl_policy=acl,
                actor_id=uuid.UUID(principal),
            ),
        )
        guide = ingest_document(
            conn,
            uuid.UUID(tenant),
            IngestionRequest(
                source_id="gate-guide",
                filename="guide.md",
                content=GUIDE_TEXT.encode(),
                knowledge_key="gate-guide",
                title="冷凝管使用说明（客户版）",
                domain="quality",
                owner_department="service",
                classification=0,
                audience="customer_public",
                required_scope="customer_public",
                acl_policy=AclPolicy.from_json(
                    {"allow": {"roles": ["customer", "staff"]}, "deny": {}}
                ),
                actor_id=uuid.UUID(principal),
            ),
        )
        for version in (quality.version_id, guide.version_id):
            service.pass_review(version, uuid.UUID(principal))
        service.approve_internal(quality.version_id, uuid.UUID(principal))
        service.approve_internal(guide.version_id, uuid.UUID(principal))
        # 对外发布必须由第二个主体执行（四眼原则），否则 ReviewConflict
        service.publish_public(guide.version_id, uuid.UUID(public_approver))
        conn.commit()

    yield {
        "tenant": tenant,
        "principal": principal,
        "public_approver": public_approver,
        "dsn": pg_url,
        "quality_version": str(quality.version_id),
        "guide_version": str(guide.version_id),
    }


def _ask(
    corpus: dict[str, Any],
    query: str,
    model_text: str,
    *,
    scope: str = "internal_product",
) -> Any:
    tenant = corpus["tenant"]
    subject = Subject(
        principal_id=str(corpus["principal"]),
        tenant_id=str(tenant),
        roles=frozenset({"staff"}),
    )
    with psycopg.connect(corpus["dsn"]) as conn:
        service = AnswerService(
            conn,  # type: ignore[arg-type]
            generator=Scripted(model_text),  # type: ignore[arg-type]
            hash_key="gate-hmac-key-0123456789",
            hash_key_id="k-gate",
            embedder=None,
            rrf_k=60,
            retrieval_mode="lexical",
        )
        return service.answer(
            query,
            knowledge_scope=scope,
            language="zh-CN",
            max_evidence=5,
            context=SearchContext(
                tenant_id=uuid.UUID(tenant),
                principal_id=uuid.UUID(corpus["principal"]),
                subject=subject,
                allowed_scopes=frozenset({scope}),
                trace_id="gate",
            ),
        )


def _citation_line(text: str) -> str:
    """从证据里取一个可用于引用的 id 需要动态查；这里由调用方传占位符。"""
    return text


def test_correct_pressure_answer_is_answered(corpus: dict[str, Any]) -> None:
    """证据里有 0.2MPa / 60 秒，答案照抄 → ANSWERED。"""
    # 先问一次拿到真实 evidence id（脚本模型第一轮返回"引用：无"）
    probe = _ask(corpus, "气密性测试充气压力是多少？", "无法回答。\n引用：无")
    assert probe.payload["status"] == str(AnswerStatus.REFUSED)

    # 用真实检索到的 chunk 作为引用来源：这里直接从库中取
    with psycopg.connect(corpus["dsn"]) as conn:
        conn.execute("SELECT set_config('app.tenant_id', %s, false)", (corpus["tenant"],))
        row = conn.execute(
            """SELECT c.id FROM knowledge_chunks c
                 JOIN knowledge_versions v ON v.tenant_id = c.tenant_id AND v.id = c.version_id
                WHERE v.id = %s AND c.chunk_kind = 'parent'""",
            (corpus["quality_version"],),
        ).fetchone()
    assert row is not None
    evidence_id = str(row[0])

    outcome = _ask(
        corpus,
        "气密性测试充气压力是多少？",
        f"气密性测试的充气压力为 0.2MPa，保压 60 秒。\n引用：{evidence_id}",
    )
    assert outcome.payload["status"] == str(AnswerStatus.ANSWERED), outcome.payload
    assert outcome.payload["citations"], "答案应当带引用"
    view = outcome.payload["literal_support"]
    assert view is not None and view["supported"] is True, view
    assert view["numbers"]["total"] >= 2, view   # 0.2MPa 与 60秒 都应被覆盖


def test_fabricated_multiplier_is_human_review(corpus: dict[str, Any]) -> None:
    """2026-11-07 历史回归：编造「1.5 倍工作压力」曾被判 ANSWERED。"""
    with psycopg.connect(corpus["dsn"]) as conn:
        conn.execute("SELECT set_config('app.tenant_id', %s, false)", (corpus["tenant"],))
        row = conn.execute(
            """SELECT c.id FROM knowledge_chunks c
                 JOIN knowledge_versions v ON v.tenant_id = c.tenant_id AND v.id = c.version_id
                WHERE v.id = %s AND c.chunk_kind = 'parent'""",
            (corpus["quality_version"],),
        ).fetchone()
    assert row is not None
    evidence_id = str(row[0])

    outcome = _ask(
        corpus,
        "气密性测试充气压力是多少？",
        "测试压力通常为设备工作压力的 1.5 倍或更高。\n引用：" + evidence_id,
    )
    payload = outcome.payload
    assert payload["status"] == str(AnswerStatus.HUMAN_REVIEW_REQUIRED), payload
    assert payload["refusal_code"] == str(RefusalCode.UNSUPPORTED_CLAIM)
    assert payload["unsupported"]["numbers"] >= 1, payload

    # 对外视图：调用方要能看出"哪个 token 没被支持"，而不是只有个数
    view = payload["literal_support"]
    assert view is not None, payload
    assert view["rule"].startswith("literal-support-v")
    assert view["applicable"] is True and view["supported"] is False
    assert "1.5倍" in view["numbers"]["unsupported"], view
    assert view["disclaimer"], "必须带'不证明语义一致'的限定说明"


def test_wrong_pressure_value_is_human_review(corpus: dict[str, Any]) -> None:
    """单位对了、数值错了（2MPa 而非 0.2MPa）→ 必须转人工。"""
    with psycopg.connect(corpus["dsn"]) as conn:
        conn.execute("SELECT set_config('app.tenant_id', %s, false)", (corpus["tenant"],))
        evidence_id = str(
            conn.execute(
                """SELECT c.id FROM knowledge_chunks c
                     JOIN knowledge_versions v ON v.tenant_id = c.tenant_id AND v.id = c.version_id
                    WHERE v.id = %s AND c.chunk_kind = 'parent'""",
                (corpus["quality_version"],),
            ).fetchone()[0]
        )
    outcome = _ask(
        corpus, "气密性测试充气压力是多少？", f"充气压力为 2MPa。\n引用：{evidence_id}"
    )
    assert outcome.payload["status"] == str(AnswerStatus.HUMAN_REVIEW_REQUIRED)
    assert outcome.payload["refusal_code"] == str(RefusalCode.UNSUPPORTED_CLAIM)


def test_answer_without_citation_is_not_answered(corpus: dict[str, Any]) -> None:
    """有证据但模型不给引用 → 不允许 ANSWERED。"""
    outcome = _ask(corpus, "气密性测试充气压力是多少？", "充气压力为 0.2MPa。\n引用：无")
    assert outcome.payload["status"] != str(AnswerStatus.ANSWERED), outcome.payload
    assert outcome.payload["citations"] == []


def test_unknown_evidence_id_is_rejected(corpus: dict[str, Any]) -> None:
    """伪造 evidence_id 不得进入引用列表。"""
    fake = str(uuid.uuid4())
    outcome = _ask(corpus, "气密性测试充气压力是多少？", f"充气压力为 0.2MPa。\n引用：{fake}")
    assert outcome.payload["status"] != str(AnswerStatus.ANSWERED)
    ids = [citation["evidence_id"] for citation in outcome.payload["citations"]]
    assert fake not in ids


def test_no_evidence_question_is_refused(corpus: dict[str, Any]) -> None:
    """知识库里没有的内容 → REFUSED / NO_EVIDENCE（不许编）。"""
    outcome = _ask(corpus, "量子计算机的超导材料是什么？", "我不知道。\n引用：无")
    assert outcome.payload["status"] == str(AnswerStatus.REFUSED), outcome.payload
    assert outcome.payload["literal_support"] is None, "非 ANSWERED 路径不执行一致性检查"
    assert outcome.payload["refusal_code"] in {
        str(RefusalCode.NO_EVIDENCE),
        str(RefusalCode.INSUFFICIENT_EVIDENCE),
    }


def test_public_content_is_visible_to_customer_scope(corpus: dict[str, Any]) -> None:
    """对外内容在 customer_public 范围可被检索到（客户视角可用）。"""
    with psycopg.connect(corpus["dsn"]) as conn:
        conn.execute("SELECT set_config('app.tenant_id', %s, false)", (corpus["tenant"],))
        evidence_id = str(
            conn.execute(
                """SELECT c.id FROM knowledge_chunks c
                     JOIN knowledge_versions v ON v.tenant_id = c.tenant_id AND v.id = c.version_id
                    WHERE v.id = %s AND c.chunk_kind = 'parent'""",
                (corpus["guide_version"],),
            ).fetchone()[0]
        )
    outcome = _ask(
        corpus,
        "售后保修期是多久？",
        f"保修期为自签收之日起 12 个月内。\n引用：{evidence_id}",
        scope="customer_public",
    )
    assert outcome.payload["status"] == str(AnswerStatus.ANSWERED), outcome.payload


def _chunk_ids(corpus: dict[str, Any], version_id: str) -> dict[str, str]:
    with psycopg.connect(corpus["dsn"]) as conn:
        conn.execute("SELECT set_config('app.tenant_id', %s, false)", (corpus["tenant"],))
        rows = conn.execute(
            """SELECT c.chunk_kind, c.id FROM knowledge_chunks c
                WHERE c.tenant_id = %s AND c.version_id = %s""",
            (corpus["tenant"], version_id),
        ).fetchall()
    return {str(kind): str(chunk) for kind, chunk in rows}


def test_citation_id_is_the_context_block_id_not_the_hit_chunk(
    corpus: dict[str, Any],
) -> None:
    """引用 id 的语义是**上下文块 id**：父块恢复后就是父块 id。

    这是集成方最容易误解的一处：模型看到的 `<evidence id>` 与检索命中的子块 id 不是同一个。
    """
    ids = _chunk_ids(corpus, corpus["quality_version"])
    parent_id, child_id = ids["parent"], ids["child"]

    # 1) 用父块 id 引用 → 合法
    ok = _ask(corpus, "气密性测试充气压力是多少？", f"充气压力为 0.2MPa。\n引用：{parent_id}")
    assert ok.payload["status"] == str(AnswerStatus.ANSWERED), ok.payload
    assert [c["evidence_id"] for c in ok.payload["citations"]] == [parent_id]

    # 2) 用真实存在、但没进入上下文块的子块 id 引用 → 被白名单丢弃
    dropped = _ask(corpus, "气密性测试充气压力是多少？", f"充气压力为 0.2MPa。\n引用：{child_id}")
    assert dropped.payload["status"] != str(AnswerStatus.ANSWERED), dropped.payload
    assert dropped.payload["citations"] == []
