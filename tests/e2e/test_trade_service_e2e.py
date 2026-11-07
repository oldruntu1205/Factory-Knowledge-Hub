"""外贸客服独立端到端验收（Rev2 §1.1-4/5、§4.5、§13；ADR-07 未签前只做只读）。

用真实 PostgreSQL + 受限身份 + 实际 HTTP，验证"外贸客服"这一侧：
1. 独立客户令牌（allowed_scopes 只有 customer_public）与内部身份互不可见；
2. 只回答**已独立公开审批**的产品事实，且引用指向该公开版本；
3. 价格 / 交期 / 质量承诺 / 定制 / MOQ（含英文）→ `HUMAN_REVIEW_REQUIRED`，答案不含价格；
4. 撤销后立即不可读（问答 / 检索 / 旧引用三路）；
5. 审计维度独立：按客户主体可追溯、`scope=customer_public`、只存 query 哈希不存明文；
6. 不产生任何对外发送副作用（ADR-07 未签 → outbox 无新事件）。
"""

from __future__ import annotations

import base64
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
import psycopg
import pytest
from fastapi.testclient import TestClient
from tests.conftest import create_principal, create_tenant

from fkh.api.main import create_app
from fkh.config import Settings

pytestmark = pytest.mark.requires_pg

SECRET = "trade-e2e-secret-0123456789abcdef"
HASH_KEY = "trade-e2e-hmac-0123456789"
HASH_KEY_ID = "k-trade"

PUBLIC_CATALOG = (
    "第 1 页\n\n货号 规格 管径*总长 上标口*下标口 单价\n"
    "A112420 200mm 38*300 24*24 39.00\n"
    "A112430 300mm 38*400 24*24 48.00\n"
)
INTERNAL_SOP = (
    "内部成本与工艺（不得对外）\n\n"
    "A112420 的出厂成本为 21.50 元，毛利目标 45%。\n"
    "内销客户可给 8 折，外贸客户最低 9 折。\n"
)


def _token(
    tenant: str,
    principal: str,
    *,
    scopes: tuple[str, ...],
    allowed: tuple[str, ...],
    roles: tuple[str, ...] = ("staff",),
    subject: str | None = None,
) -> str:
    now = datetime.now(UTC)
    payload = {
        "iss": "fkh.local",
        "aud": "fkh-api",
        "sub": subject or f"{principal[:8]}@example.test",
        "iat": now,
        "exp": now + timedelta(minutes=10),
        "tenant_id": tenant,
        "principal_id": principal,
        "roles": list(roles),
        "scope": list(scopes),
        "allowed_scopes": list(allowed),
    }
    return jwt.encode(payload, SECRET, algorithm="HS256")


class _Scripted:
    """脚本模型：分类器答"否"，生成时引用 prompt 里的第一个证据 id。"""

    model_id = "scripted"

    def __init__(self, answer: str) -> None:
        self._answer = answer

    def generate(self, prompt: str, *, max_tokens: int = 512, temperature: float = 0.0) -> Any:
        from dataclasses import dataclass as _dc

        @_dc
        class _Generated:
            text: str
            model_id: str = "scripted"

        if "意图分类器" in prompt:
            return _Generated(text="否")
        import re as _re

        found = _re.search(r'<evidence id="([0-9a-f-]{36})"', prompt)
        cite = found.group(1) if found else "无"
        return _Generated(text=f"{self._answer}\n引用：{cite}")


@pytest.fixture
def client(pg_url: str) -> Iterator[TestClient]:
    settings = Settings(
        jwt_secret=SECRET,
        database_url=pg_url,
        retrieval_hash_key=HASH_KEY,
        retrieval_hash_key_id=HASH_KEY_ID,
    )
    with TestClient(create_app(settings), raise_server_exceptions=False) as test_client:
        yield test_client


@pytest.fixture
def parties(admin: psycopg.Connection[Any]) -> dict[str, Any]:
    tenant = create_tenant(admin, f"外贸 {uuid.uuid4().hex[:6]}")
    ops = create_principal(admin, tenant, f"ops-{uuid.uuid4().hex[:6]}@example.test")
    reviewer = create_principal(admin, tenant, f"rev-{uuid.uuid4().hex[:6]}@example.test")
    publisher = create_principal(admin, tenant, f"pub-{uuid.uuid4().hex[:6]}@example.test")
    customer = create_principal(admin, tenant, f"buyer-{uuid.uuid4().hex[:6]}@example.test")
    admin.commit()
    return {
        "tenant": tenant,
        "customer_principal": customer,
        "customer_subject": f"buyer-{uuid.uuid4().hex[:6]}",
        "ops_token": _token(
            tenant, ops,
            scopes=("knowledge.ingest", "knowledge.read", "knowledge.review", "knowledge.publish",
                    "knowledge.revoke", "knowledge.answer", "knowledge.search", "catalog.read"),
            allowed=("internal_product", "customer_public"),
        ),
        "reviewer_token": _token(
            tenant, reviewer, scopes=("knowledge.review",), allowed=("customer_public",)
        ),
        "publisher_token": _token(
            tenant, publisher, scopes=("knowledge.publish_public",), allowed=("customer_public",),
            roles=("public_approver",),
        ),
        "customer_token": _token(
            tenant, customer,
            scopes=("knowledge.answer", "knowledge.search", "evidence.read"),
            allowed=("customer_public",), roles=("customer",),
        ),
    }


def _h(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _upload(client: TestClient, token: str, *, text: str, title: str, cls: int = 0,
            audience: str = "customer_public", scope: str = "customer_public") -> str:
    body = {
        "filename": f"{uuid.uuid4().hex[:6]}.md",
        "title": title,
        "domain": "product_catalog" if cls == 0 else "quality",
        "owner_department": "sales",
        "classification": cls,
        "audience": audience,
        "required_scope": scope,
        "acl_allow_roles": ["customer", "staff"],
        "acl_deny_roles": [],
        "knowledge_key": f"k-{uuid.uuid4().hex[:8]}",
        "content_base64": base64.b64encode(text.encode()).decode(),
        "catalog_currency": "CNY" if cls == 0 else None,
    }
    response = client.post("/v1/knowledge/documents", json=body, headers=_h(token))
    assert response.status_code == 201, response.text
    return response.json()["version_id"]


def _publish_public(client: TestClient, parties: dict[str, Any], version_id: str) -> None:
    assert client.post(
        f"/v1/knowledge/{version_id}/review", json={"decision": "PASS"},
        headers=_h(parties["reviewer_token"]),
    ).status_code == 200
    assert client.post(
        f"/v1/knowledge/{version_id}/publish", json={"audience": "internal"},
        headers=_h(parties["ops_token"]),
    ).status_code == 200
    published = client.post(
        f"/v1/knowledge/{version_id}/publish", json={"audience": "customer_public"},
        headers=_h(parties["publisher_token"]),
    )
    assert published.status_code == 200, published.text
    assert published.json()["status"] == "PUBLISHED_PUBLIC"


def _ask(client: TestClient, token: str, query: str) -> Any:
    return client.post(
        "/v1/knowledge/answer",
        json={"query": query, "knowledge_scope": "customer_public", "language": "zh-CN",
              "max_evidence": 5},
        headers=_h(token),
    )


def test_trade_service_answers_public_facts_only(
    client: TestClient, parties: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "fkh.api.main.get_generator",
        lambda *a, **k: _Scripted("该型号规格为 200mm 38*300 24*24。"),
    )
    public = _upload(client, parties["ops_token"], text=PUBLIC_CATALOG, title="对外目录 C0")
    _publish_public(client, parties, public)
    internal = _upload(
        client, parties["ops_token"], text=INTERNAL_SOP, title="内部成本", cls=1,
        audience="internal", scope="internal_product",
    )
    assert client.post(
        f"/v1/knowledge/{internal}/review", json={"decision": "PASS"},
        headers=_h(parties["reviewer_token"]),
    ).status_code == 200

    answer = _ask(client, parties["customer_token"], "A112420 的规格是什么？")
    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert body["status"] == "ANSWERED", body
    cited_versions = {c["version_id"] for c in body["citations"]}
    assert cited_versions == {public}, "引用只能来自已公开版本"
    assert "21.50" not in body["answer"], "内部成本不得出现"

    leak = _ask(client, parties["customer_token"], "A112420 的出厂成本是多少？")
    assert leak.json()["status"] != "ANSWERED", leak.json()
    assert "21.50" not in leak.json().get("answer", "")


def test_commitment_questions_go_to_human_in_zh_and_en(
    client: TestClient, parties: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "fkh.api.main.get_generator", lambda *a, **k: _Scripted("39.00 元。")
    )
    public = _upload(client, parties["ops_token"], text=PUBLIC_CATALOG, title="对外目录 C0")
    _publish_public(client, parties, public)

    cases = (
        "A112420 的单价是多少？",
        "这款冷凝管多少钱？",
        "下单后多久能交货？",
        "你们能保证三年内不炸裂吗？",
        "能不能按我们的图纸定制？",
        "What is the MOQ and lead time?",
        "Can you give me a price for 1000 pcs?",
    )
    for query in cases:
        response = _ask(client, parties["customer_token"], query)
        assert response.status_code == 200, (query, response.text)
        body = response.json()
        assert body["status"] == "HUMAN_REVIEW_REQUIRED", (query, body)
        assert body["refusal_code"] == "HUMAN_REVIEW_REQUIRED", (query, body)
        assert "39.00" not in body.get("answer", ""), (query, body)


def test_revoke_takes_effect_for_trade_service_immediately(
    client: TestClient, parties: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "fkh.api.main.get_generator",
        lambda *a, **k: _Scripted("该型号规格为 200mm 38*300 24*24。"),
    )
    public = _upload(client, parties["ops_token"], text=PUBLIC_CATALOG, title="对外目录 C0")
    _publish_public(client, parties, public)

    before = _ask(client, parties["customer_token"], "A112420 的规格是什么？")
    assert before.json()["status"] == "ANSWERED", before.json()
    old_citation = before.json()["citations"][0]["evidence_id"]

    revoked = client.post(
        f"/v1/knowledge/{public}/revoke?reason=trade-e2e",
        headers=_h(parties["ops_token"]),
    )
    assert revoked.status_code == 200, revoked.text

    after = _ask(client, parties["customer_token"], "A112420 的规格是什么？")
    assert after.json()["status"] != "ANSWERED", after.json()
    search = client.post(
        "/v1/knowledge/search",
        json={"query": "A112420", "knowledge_scope": "customer_public", "max_results": 5},
        headers=_h(parties["customer_token"]),
    )
    assert search.json()["results"] == []
    stale = client.get(f"/v1/evidence/{old_citation}", headers=_h(parties["customer_token"]))
    assert stale.status_code == 403


def test_audit_dimension_is_per_customer_and_hashed(
    client: TestClient, parties: dict[str, Any], pg_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "fkh.api.main.get_generator",
        lambda *a, **k: _Scripted("该型号规格为 200mm 38*300 24*24。"),
    )
    public = _upload(client, parties["ops_token"], text=PUBLIC_CATALOG, title="对外目录 C0")
    _publish_public(client, parties, public)
    query = "A112420 的规格是什么？"
    assert _ask(client, parties["customer_token"], query).status_code == 200

    with psycopg.connect(pg_url) as conn:
        conn.execute("SELECT set_config('app.tenant_id', %s, false)", (parties["tenant"],))
        row = conn.execute(
            "SELECT principal_id, scope, query_hash, status, route"
            " FROM rag_queries WHERE tenant_id = %s ORDER BY created_at DESC LIMIT 1",
            (parties["tenant"],),
        ).fetchone()
        plaintext_hits = conn.execute(
            "SELECT count(*) FROM rag_queries WHERE tenant_id = %s AND query_hash LIKE %s",
            (parties["tenant"], f"%{query}%"),
        ).fetchone()[0]

    assert row is not None
    assert str(row[0]) == parties["customer_principal"], "审计必须按客户主体可追溯"
    assert row[1] == "customer_public"
    assert row[2] and query not in row[2], "只存哈希，不得落明文"
    assert plaintext_hits == 0


def test_no_outbound_side_effect_without_adr07(
    client: TestClient, parties: dict[str, Any], pg_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADR-07 未签：外贸客服只读，不得产生对外发送事件。"""
    monkeypatch.setattr(
        "fkh.api.main.get_generator",
        lambda *a, **k: _Scripted("该型号规格为 200mm 38*300 24*24。"),
    )
    public = _upload(client, parties["ops_token"], text=PUBLIC_CATALOG, title="对外目录 C0")
    _publish_public(client, parties, public)
    with psycopg.connect(pg_url) as conn:
        conn.execute("SELECT set_config('app.tenant_id', %s, false)", (parties["tenant"],))
        before = conn.execute(
            "SELECT count(*) FROM outbox_events WHERE tenant_id = %s", (parties["tenant"],)
        ).fetchone()[0]

    assert _ask(client, parties["customer_token"], "A112420 的规格是什么？").status_code == 200
    assert _ask(client, parties["customer_token"], "多少钱？").status_code == 200

    with psycopg.connect(pg_url) as conn:
        conn.execute("SELECT set_config('app.tenant_id', %s, false)", (parties["tenant"],))
        after = conn.execute(
            "SELECT count(*) FROM outbox_events WHERE tenant_id = %s", (parties["tenant"],)
        ).fetchone()[0]
    assert after == before, "问答不得产生对外发送事件"


def test_customer_cannot_reach_internal_domains(
    client: TestClient, parties: dict[str, Any]
) -> None:
    """跨域隔离：客户不能读文档管理面、不能读培训正文、不能用内部范围问。"""
    assert client.get(
        "/v1/knowledge/documents", headers=_h(parties["customer_token"])
    ).status_code == 403

    internal_scope = client.post(
        "/v1/knowledge/answer",
        json={"query": "A112420 的规格", "knowledge_scope": "internal_product",
              "language": "zh-CN", "max_evidence": 3},
        headers=_h(parties["customer_token"]),
    )
    assert internal_scope.status_code == 403

    assert client.get(
        f"/v1/training/assignments/{uuid.uuid4()}/content",
        headers=_h(parties["customer_token"]),
    ).status_code == 403
