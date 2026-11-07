"""带引用问答端到端：状态判定、引用以系统为准、审计、能力故障口径。

逐条对应 2026-11-06 L1 裁决 §4 的十项断言（见 `_evidence/l1_reply_answer_generation_20261106.md`）。
用确定性生成器替换本地模型：本文件验证编排与边界，不验证模型质量。
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import jwt
import psycopg
import pytest
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator, FormatChecker
from tests.conftest import (
    create_acl,
    create_document,
    create_principal,
    create_tenant,
    create_version,
)

from fkh.api.main import create_app
from fkh.config import Settings
from fkh.domain.lexeme import segment_cjk
from fkh.generation import DeterministicGenerator, GenerationError

pytestmark = pytest.mark.requires_pg

SECRET = "answer-api-secret-0123456789abcdef"
HASH_KEY = "answer-audit-hmac-key-0123456789"
HASH_KEY_ID = "k-answer"
ROOT = Path(__file__).resolve().parents[2]
ANSWER_SCHEMA = json.loads(
    (ROOT / "contracts" / "schema" / "answer_response.json").read_text(encoding="utf-8")
)
TEXT = "冷凝管规格说明：高硼硅玻璃，接口 24/29 与 29/32。"


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------
def _token(
    tenant_id: str,
    principal_id: str,
    *,
    scopes: tuple[str, ...] = ("knowledge.answer", "knowledge.search"),
    allowed: tuple[str, ...] = ("internal_product",),
) -> str:
    now = datetime.now(UTC)
    payload = {
        "iss": "fkh.local",
        "aud": "fkh-api",
        "sub": f"user-{principal_id[:8]}@example.test",
        "iat": now,
        "exp": now + timedelta(minutes=10),
        "tenant_id": tenant_id,
        "principal_id": principal_id,
        "roles": ["staff"],
        "scope": list(scopes),
        "allowed_scopes": list(allowed),
    }
    return jwt.encode(payload, SECRET, algorithm="HS256")


def _settings(pg_url: str, **overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "jwt_secret": SECRET,
        "database_url": pg_url,
        "retrieval_hash_key": HASH_KEY,
        "retrieval_hash_key_id": HASH_KEY_ID,
    }
    base.update(overrides)
    return Settings(**base)


@pytest.fixture
def client(pg_url: str) -> Iterator[TestClient]:
    with TestClient(create_app(_settings(pg_url)), raise_server_exceptions=False) as test_client:
        yield test_client


@pytest.fixture
def tenant(admin: psycopg.Connection[Any]) -> str:
    tenant_id = create_tenant(admin, f"问答租户 {uuid.uuid4().hex[:6]}")
    admin.commit()
    return tenant_id


def _chunk(
    admin: psycopg.Connection[Any],
    tenant_id: str,
    version_id: str,
    acl_id: str,
    *,
    text: str = TEXT,
    kind: str = "child",
    parent_id: str | None = None,
) -> str:
    row = admin.execute(
        """INSERT INTO knowledge_chunks
             (tenant_id, version_id, parent_chunk_id, chunk_kind, chunk_text, chunk_sha256,
              tokenizer_id, lexeme_tokenizer_id, heading_path, locator, acl_policy_id,
              index_generation, lexeme)
           VALUES (%s, %s, %s, %s, %s, %s, 'approx-cjk-v1', 'cjk-char-v1',
                   ARRAY['第 1 章'], %s::jsonb, %s, 1, to_tsvector('simple', %s))
           RETURNING id""",
        (
            tenant_id,
            version_id,
            parent_id,
            kind,
            text,
            hashlib.sha256(text.encode()).hexdigest(),
            json.dumps({"block_index": 0, "page_start": 1}),
            acl_id,
            segment_cjk(text),
        ),
    ).fetchone()
    assert row is not None
    return str(row[0])


def _seed(admin: psycopg.Connection[Any], tenant_id: str) -> dict[str, str]:
    principal = create_principal(admin, tenant_id, f"asker-{uuid.uuid4().hex[:6]}@example.test")
    approver = create_principal(admin, tenant_id, f"appr-{uuid.uuid4().hex[:6]}@example.test")
    doc = create_document(admin, tenant_id, f"doc-{uuid.uuid4().hex[:8]}")
    version = create_version(
        admin, tenant_id, doc, status="APPROVED_INTERNAL", approved_by=approver
    )
    admin.execute(
        "UPDATE knowledge_versions SET required_scope = 'internal_product' WHERE id = %s",
        (version,),
    )
    acl = create_acl(admin, tenant_id)
    chunk = _chunk(admin, tenant_id, version, acl)
    admin.commit()
    return {
        "principal": principal,
        "version": version,
        "chunk": chunk,
        "token": _token(tenant_id, principal),
    }


def _ask(client: TestClient, token: str, query: str = "冷凝管接口", **overrides: Any) -> Any:
    payload: dict[str, Any] = {
        "query": query,
        "knowledge_scope": "internal_product",
        "language": "zh-CN",
        "max_evidence": 5,
    }
    payload.update(overrides)
    return client.post(
        "/v1/knowledge/answer", json=payload, headers={"Authorization": f"Bearer {token}"}
    )


def _use_generator(monkeypatch: pytest.MonkeyPatch, text: str = "") -> DeterministicGenerator:
    generator = DeterministicGenerator(text)
    monkeypatch.setattr("fkh.api.main.get_generator", lambda *args, **kwargs: generator)
    return generator


def _audit_rows(admin: psycopg.Connection[Any], tenant_id: str) -> list[tuple[Any, ...]]:
    return admin.execute(
        """SELECT status, refusal_code, model_id, query_hash, scope, trace_id, id
             FROM rag_queries WHERE tenant_id = %s ORDER BY created_at""",
        (tenant_id,),
    ).fetchall()


def _citations(admin: psycopg.Connection[Any], query_id: Any) -> int:
    row = admin.execute(
        "SELECT count(*) FROM rag_citations WHERE query_id = %s", (query_id,)
    ).fetchone()
    return int(row[0]) if row else 0


# ---------------------------------------------------------------------------
# 1. 正常作答：引用以系统为准，响应过契约
# ---------------------------------------------------------------------------
def test_answer_answered_with_system_side_citations(
    client: TestClient,
    admin: psycopg.Connection[Any],
    tenant: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seeded = _seed(admin, tenant)
    _use_generator(monkeypatch, f"标准接口为 24/29 与 29/32。[{seeded['chunk']}]")

    response = _ask(client, seeded["token"])
    assert response.status_code == 200, response.text
    body = response.json()

    Draft202012Validator(ANSWER_SCHEMA, format_checker=FormatChecker()).validate(body)
    assert body["status"] == "ANSWERED", body
    assert body["requires_human_review"] is False
    assert len(body["citations"]) == 1
    citation = body["citations"][0]
    assert citation["evidence_id"] == seeded["chunk"]
    assert citation["version_id"] == seeded["version"]
    assert citation["locator"] == {"block_index": 0, "page_start": 1}
    assert body["index_generation"] == 1

    rows = _audit_rows(admin, tenant)
    assert len(rows) == 1
    assert rows[0][0] == "ANSWERED"
    assert rows[0][2] and rows[0][2].startswith("test-deterministic@")
    assert _citations(admin, rows[0][6]) == 1


# ---------------------------------------------------------------------------
# 2. 引用核验的三条分支
# ---------------------------------------------------------------------------
def test_forged_citation_requires_human_review(
    client: TestClient, admin: psycopg.Connection[Any], tenant: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    seeded = _seed(admin, tenant)
    forged = "00000000-0000-0000-0000-000000000000"
    _use_generator(
        monkeypatch, f"接口为 24/29。[{seeded['chunk']}] 另有补充。[{forged}]"
    )

    body = _ask(client, seeded["token"]).json()
    assert body["status"] == "HUMAN_REVIEW_REQUIRED"
    assert body["requires_human_review"] is True
    assert body["refusal_code"] == "HUMAN_REVIEW_REQUIRED"
    assert [c["evidence_id"] for c in body["citations"]] == [seeded["chunk"]], "可用引用不得被静默丢弃"


def test_zero_valid_citations_is_refused(
    client: TestClient, admin: psycopg.Connection[Any], tenant: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    seeded = _seed(admin, tenant)
    _use_generator(monkeypatch, "接口为 24/29。[11111111-1111-1111-1111-111111111111]")

    body = _ask(client, seeded["token"]).json()
    assert body["status"] == "REFUSED"
    assert body["refusal_code"] == "INSUFFICIENT_EVIDENCE"
    assert body["requires_human_review"] is True


def test_unmarked_sentences_no_longer_block_answer(
    client: TestClient, admin: psycopg.Connection[Any], tenant: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """2026-11-06 业务决策 A：逐句覆盖不再是闸门，但引用必须真实存在。

    这条用例固定"放宽后的行为"：正文里可以有未标注的句子，只要引用清单里给了合法 id。
    """
    seeded = _seed(admin, tenant)
    _use_generator(
        monkeypatch,
        f"这批货下周一定可以发货，价格还能再降三成。接口为 24/29。\n引用：{seeded['chunk']}",
    )

    body = _ask(client, seeded["token"]).json()
    assert body["status"] == "ANSWERED"
    assert body["citations"][0]["evidence_id"] == seeded["chunk"]
    assert "引用：" not in body["answer"], "引用清单属于元数据，不应出现在展示正文里"


def test_citation_list_protocol_uses_system_side_locator(
    client: TestClient, admin: psycopg.Connection[Any], tenant: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """新协议：正文 + 末尾「引用：id」；version_id/locator 仍取系统侧。"""
    seeded = _seed(admin, tenant)
    _use_generator(monkeypatch, f"标准接口为 24/29 与 29/32。\n引用：{seeded['chunk']}")

    body = _ask(client, seeded["token"]).json()
    assert body["status"] == "ANSWERED"
    assert body["answer"].strip() == "标准接口为 24/29 与 29/32。"
    assert body["citations"][0]["version_id"] == seeded["version"]
    assert body["citations"][0]["locator"] == {"block_index": 0, "page_start": 1}


def test_citation_list_none_is_refused(
    client: TestClient, admin: psycopg.Connection[Any], tenant: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    seeded = _seed(admin, tenant)
    _use_generator(monkeypatch, "无法从现有资料回答。\n引用：无")
    body = _ask(client, seeded["token"]).json()
    assert body["status"] == "REFUSED"
    assert body["refusal_code"] == "INSUFFICIENT_EVIDENCE"


def test_forged_id_in_citation_list_still_requires_review(
    client: TestClient, admin: psycopg.Connection[Any], tenant: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    seeded = _seed(admin, tenant)
    forged = "00000000-0000-0000-0000-000000000000"
    _use_generator(monkeypatch, f"接口为 24/29。\n引用：{seeded['chunk']} {forged}")
    body = _ask(client, seeded["token"]).json()
    assert body["status"] == "HUMAN_REVIEW_REQUIRED"
    assert [c["evidence_id"] for c in body["citations"]] == [seeded["chunk"]]


# ---------------------------------------------------------------------------
# 3. 无证据：不调用生成
# ---------------------------------------------------------------------------
def test_no_evidence_does_not_call_generator(
    client: TestClient, admin: psycopg.Connection[Any], tenant: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    seeded = _seed(admin, tenant)
    generator = _use_generator(monkeypatch, "不应被调用")

    body = _ask(client, seeded["token"], query="量子纠缠退火炉").json()
    assert body["status"] == "REFUSED"
    assert body["refusal_code"] == "NO_EVIDENCE"
    assert body["requires_human_review"] is False
    assert generator.calls == [], "没有证据时不得调用模型"
    assert _audit_rows(admin, tenant)[0][0] == "REFUSED"


# ---------------------------------------------------------------------------
# 4. 能力故障：503 + ERROR 审计，且不泄漏内部信息
# ---------------------------------------------------------------------------
def test_generation_failure_returns_503_and_audits_error(
    client: TestClient, admin: psycopg.Connection[Any], tenant: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    seeded = _seed(admin, tenant)

    class Boom(DeterministicGenerator):
        def generate(self, prompt: str, *, max_tokens: int, temperature: float) -> Any:
            raise GenerationError("本地推理不可达：连接被拒绝")

    monkeypatch.setattr("fkh.api.main.get_generator", lambda *a, **k: Boom(""))

    response = _ask(client, seeded["token"])
    assert response.status_code == 503
    error = response.json()["error"]
    assert error["code"] == "SERVICE_UNAVAILABLE"
    assert error["trace_id"]
    assert "本地推理" not in error["message"], "响应体不得暴露内部故障细节"

    rows = _audit_rows(admin, tenant)
    assert rows and rows[0][0] == "ERROR", "能力故障必须留 ERROR 审计"
    assert rows[0][1] is None


def test_provider_none_returns_503_and_audits_error(
    client: TestClient, admin: psycopg.Connection[Any], tenant: str
) -> None:
    seeded = _seed(admin, tenant)
    response = _ask(client, seeded["token"])
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "SERVICE_UNAVAILABLE"
    rows = _audit_rows(admin, tenant)
    assert rows and rows[0][0] == "ERROR"


def test_scope_denied_is_403_even_when_provider_missing(
    client: TestClient, admin: psycopg.Connection[Any], tenant: str
) -> None:
    """越权不得被"配置没配好"掩盖成 503（裁决 §第 1 项优先级）。"""
    seeded = _seed(admin, tenant)
    token = _token(tenant, seeded["principal"], allowed=("internal_training",))
    response = _ask(client, token)
    assert response.status_code == 403


# ---------------------------------------------------------------------------
# 5. 审计：不落明文、引用按条记账
# ---------------------------------------------------------------------------
def test_audit_keeps_hash_only_and_citations_count_matches(
    client: TestClient, admin: psycopg.Connection[Any], tenant: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    seeded = _seed(admin, tenant)
    _use_generator(monkeypatch, f"接口为 24/29。[{seeded['chunk']}]")
    query = "冷凝管接口是什么"

    body = _ask(client, seeded["token"], query=query).json()
    assert body["status"] == "ANSWERED"

    rows = _audit_rows(admin, tenant)
    assert len(rows) == 1
    status, refusal, model_id, query_hash, scope, trace_id, query_id = rows[0]
    assert query_hash != query and len(query_hash) == 64, "只存 HMAC 摘要"
    dumped = json.dumps([[str(value) for value in row] for row in rows], ensure_ascii=False)
    assert query not in dumped, "任何字段都不得含 query 明文"
    assert scope == "internal_product"
    assert trace_id == body["trace_id"]
    assert _citations(admin, query_id) == len(body["citations"])


# ---------------------------------------------------------------------------
# 6. 语言进入生成指令
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("language", "marker"),
    [("zh-CN", "简体中文"), ("en-US", "English")],
)
def test_language_reaches_prompt(
    client: TestClient,
    admin: psycopg.Connection[Any],
    tenant: str,
    monkeypatch: pytest.MonkeyPatch,
    language: str,
    marker: str,
) -> None:
    seeded = _seed(admin, tenant)
    generator = _use_generator(monkeypatch, f"接口为 24/29。[{seeded['chunk']}]")
    _ask(client, seeded["token"], language=language)
    assert generator.calls, "应当调用过一次生成"
    assert marker in generator.calls[0]


# ---------------------------------------------------------------------------
# 7. 父块恢复不得把越权内容带进上下文（L1 终审第 2 条）
# ---------------------------------------------------------------------------
def test_parent_restore_excludes_unauthorized_parent_content(
    client: TestClient, admin: psycopg.Connection[Any], tenant: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """命中授权子块、但父块本身无权读取：上下文与引用都只能是子块。"""
    principal = create_principal(admin, tenant, f"asker-{uuid.uuid4().hex[:6]}@example.test")
    approver = create_principal(admin, tenant, f"appr-{uuid.uuid4().hex[:6]}@example.test")
    doc = create_document(admin, tenant, f"doc-{uuid.uuid4().hex[:8]}")
    version = create_version(admin, tenant, doc, status="APPROVED_INTERNAL", approved_by=approver)
    admin.execute(
        "UPDATE knowledge_versions SET required_scope = 'internal_product' WHERE id = %s",
        (version,),
    )
    acl_staff = create_acl(admin, tenant)  # 允许 staff
    acl_manager = create_acl(admin, tenant, {"allow": {"roles": ["manager"]}, "deny": {}})

    secret = "内部成本价 3.5 元，禁止外传。"
    parent = _chunk(admin, tenant, version, acl_manager, text=f"冷凝管资料。{secret}", kind="parent")
    child = _chunk(admin, tenant, version, acl_staff, text=TEXT, parent_id=parent)
    admin.commit()

    generator = _use_generator(monkeypatch, f"接口为 24/29。[{child}]")
    token = _token(tenant, principal)
    body = _ask(client, token).json()

    assert body["status"] == "ANSWERED"
    prompt = generator.calls[0]
    assert "内部成本价" not in prompt, "父块内容不得进入生成上下文"
    assert secret not in prompt
    assert [c["evidence_id"] for c in body["citations"]] == [child]
    assert parent not in {c["evidence_id"] for c in body["citations"]}


def test_inline_marker_is_rendered_as_readable_index(
    client: TestClient, admin: psycopg.Connection[Any], tenant: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """裁决：展示层不得裸露内部 chunk id，内联标记转成 [1]/[2]。"""
    seeded = _seed(admin, tenant)
    _use_generator(monkeypatch, f"标准接口为 24/29 与 29/32。[{seeded['chunk']}]")
    body = _ask(client, seeded["token"]).json()
    assert body["status"] == "ANSWERED"
    assert "[1]" in body["answer"]
    assert seeded["chunk"] not in body["answer"], "不得把内部 id 直接展示给用户"


def test_explicit_none_with_valid_inline_still_answers(
    client: TestClient, admin: psycopg.Connection[Any], tenant: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """清单写"无"但正文有合法内联引用：以合并结果为准（协议冲突只记日志）。"""
    seeded = _seed(admin, tenant)
    _use_generator(monkeypatch, f"标准接口为 24/29。[{seeded['chunk']}]\n引用：无")
    body = _ask(client, seeded["token"]).json()
    assert body["status"] == "ANSWERED"
    assert [c["evidence_id"] for c in body["citations"]] == [seeded["chunk"]]


def test_generator_construction_failure_is_503_not_500(
    client: TestClient, admin: psycopg.Connection[Any], tenant: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """本地推理不可达发生在**构造期**（OllamaGenerator 探测 /api/tags）时，也必须 503。

    这条 bug 是实测撞出来的：模型没起来时接口回 500，看起来像服务端 bug，
    与 SPEC「能力故障 = 503 + ERROR 审计」相反。
    """
    seeded = _seed(admin, tenant)

    def boom(*args: Any, **kwargs: Any) -> Any:
        raise GenerationError("本地推理不可达：连接被拒绝")

    monkeypatch.setattr("fkh.api.main.get_generator", boom)

    response = _ask(client, seeded["token"])
    assert response.status_code == 503
    body = response.json()
    assert body["error"]["code"] == "SERVICE_UNAVAILABLE"
