"""查询侧货号识别按实例规则（含 EAN 纯数字）的真实 PostgreSQL 验收。

背景（L1 2026-11-08）：替换正则只能证明"抽取/入库支持新货号"，不等于"查询识别已支持"；
纯数字 EAN 与价格/数量可能重叠，因此本轮：
1. 查询候选由**实例规则** fullmatch 判定（不再只认工厂正则）；
2. 加数量/单位语境排除（"要 3 个…"、"… 元"）；
3. 仍是"恰好一个货号 + 明确询价/询规格"才接管，避免抢答。
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
from tests.e2e._roles import drop_business_role, ensure_business_role

from fkh.api.main import create_app
from fkh.config import Settings

pytestmark = pytest.mark.requires_pg

SECRET = "ean-query-secret-0123456789ab"
HASH_KEY = "ean-query-hmac-0123456789"
HASH_KEY_ID = "k-ean"

EAN_RULE = r"^\d{8,14}$"
EAN_TEXT = "第 1 页\n\n6901234567892 500ml 玻璃量筒 128.00\n"


class _Scripted:
    model_id = "scripted"

    def generate(self, prompt: str, *, max_tokens: int = 512, temperature: float = 0.0) -> Any:
        from dataclasses import dataclass as _dc

        @_dc
        class _Generated:
            text: str
            model_id: str = "scripted"

        if "意图分类器" in prompt:
            return _Generated(text="否")
        return _Generated(text="无法回答。\n引用：无")


def _token(tenant: str, principal: str, scopes: tuple[str, ...]) -> str:
    now = datetime.now(UTC)
    return jwt.encode(
        {
            "iss": "fkh.local", "aud": "fkh-api", "sub": f"{principal[:8]}@ean.test",
            "iat": now, "exp": now + timedelta(minutes=20), "tenant_id": tenant,
            "principal_id": principal, "roles": ["staff"], "scope": list(scopes),
            "allowed_scopes": ["internal_product", "customer_public"],
        },
        SECRET,
        algorithm="HS256",
    )


@pytest.fixture(scope="module")
def role(pg_url: str) -> Iterator[str]:
    name, dsn = ensure_business_role(pg_url, prefix="fkh_ean")
    yield dsn
    drop_business_role(pg_url, name)


@pytest.fixture
def client(role: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setattr("fkh.api.main.get_generator", lambda *a, **k: _Scripted())
    monkeypatch.setattr("fkh.api.answer_exec.get_generator", lambda *a, **k: _Scripted())
    settings = Settings(jwt_secret=SECRET, database_url=role,
                        retrieval_hash_key=HASH_KEY, retrieval_hash_key_id=HASH_KEY_ID)
    with TestClient(create_app(settings), raise_server_exceptions=False) as test_client:
        yield test_client


def _h(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def world(client: TestClient, admin: psycopg.Connection[Any]) -> dict[str, Any]:
    tenant = create_tenant(admin, f"EAN {uuid.uuid4().hex[:6]}")
    principal = create_principal(admin, tenant, f"e-{uuid.uuid4().hex[:6]}@ean.test")
    admin.commit()
    writer = _token(tenant, principal, ("knowledge.read", "knowledge.write", "knowledge.ingest",
                                        "knowledge.review", "knowledge.publish",
                                        "knowledge.answer", "knowledge.search", "evidence.read"))
    agent = _token(tenant, principal, ("knowledge.answer", "knowledge.search", "evidence.read"))

    # 实例规则：EAN 纯数字
    put = client.put("/v1/config/catalog-rules",
                     json={"code_pattern": EAN_RULE, "currencies": ["CNY"]},
                     headers=_h(writer))
    assert put.status_code == 200, put.text

    body = {
        "filename": "ean.md", "title": "量筒目录", "domain": "product_catalog",
        "owner_department": "sales", "classification": 1, "audience": "internal",
        "required_scope": "internal_product", "acl_allow_roles": ["staff"],
        "knowledge_key": f"ean-{uuid.uuid4().hex[:8]}",
        "content_base64": base64.b64encode(EAN_TEXT.encode()).decode(),
        "catalog_currency": "CNY",
    }
    uploaded = client.post("/v1/knowledge/documents", json=body, headers=_h(writer))
    assert uploaded.status_code == 201, uploaded.text
    version_id = uploaded.json()["version_id"]
    assert client.post(f"/v1/knowledge/{version_id}/review", json={"decision": "PASS"},
                       headers=_h(writer)).status_code == 200
    assert client.post(f"/v1/knowledge/{version_id}/publish", json={"audience": "internal"},
                       headers=_h(writer)).status_code == 200
    return {"tenant": tenant, "version_id": version_id, "agent": agent, "writer": writer}


def test_ean_code_question_is_answered_by_catalog_channel(
    client: TestClient, world: dict[str, Any]
) -> None:
    answer = client.post(
        "/v1/knowledge/answer",
        json={"query": "6901234567892 的规格是什么？", "knowledge_scope": "internal_product",
              "language": "zh-CN", "max_evidence": 5},
        headers=_h(world["agent"]),
    )
    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert body["status"] == "ANSWERED", body
    assert "500ml" in body["answer"], body
    assert body["citations"], body
    assert body["citations"][0]["version_id"] == world["version_id"]


def test_quantity_context_is_not_treated_as_code(
    client: TestClient, world: dict[str, Any], admin: psycopg.Connection[Any]
) -> None:
    """"要 3 个 6901234567892" 里的长数字可能是数量语境，不能抢答成货号。"""
    answer = client.post(
        "/v1/knowledge/answer",
        json={"query": "这次要 3 个 6901234567892 的规格吗？",
              "knowledge_scope": "internal_product", "language": "zh-CN",
              "max_evidence": 5},
        headers=_h(world["agent"]),
    )
    assert answer.status_code == 200, answer.text

    with psycopg.connect(admin.info.dsn) as conn:
        conn.execute("SELECT set_config('app.tenant_id', %s, false)", (world["tenant"],))
        routes = conn.execute(
            "SELECT route, route_reason FROM rag_queries WHERE tenant_id = %s"
            " ORDER BY created_at DESC LIMIT 1",
            (world["tenant"],),
        ).fetchone()
    assert routes is not None
    assert routes[0] != "catalog_exact", routes


def test_factory_tenant_still_detects_factory_codes(
    client: TestClient, admin: psycopg.Connection[Any]
) -> None:
    """未改规则的工厂租户行为不变（回归）。"""
    tenant = create_tenant(admin, f"FACT {uuid.uuid4().hex[:6]}")
    principal = create_principal(admin, tenant, f"f-{uuid.uuid4().hex[:6]}@ean.test")
    admin.commit()
    writer = _token(tenant, principal, ("knowledge.read", "knowledge.write", "knowledge.ingest",
                                        "knowledge.review", "knowledge.publish",
                                        "knowledge.answer", "knowledge.search", "evidence.read"))
    body = {
        "filename": "fact.md", "title": "工厂目录", "domain": "product_catalog",
        "owner_department": "sales", "classification": 1, "audience": "internal",
        "required_scope": "internal_product", "acl_allow_roles": ["staff"],
        "knowledge_key": f"fact-{uuid.uuid4().hex[:8]}",
        "content_base64": base64.b64encode("第 1 页\n\nA112420 200mm 39.00\n".encode()).decode(),
        "catalog_currency": "CNY",
    }
    uploaded = client.post("/v1/knowledge/documents", json=body, headers=_h(writer))
    assert uploaded.status_code == 201, uploaded.text
    version_id = uploaded.json()["version_id"]
    client.post(f"/v1/knowledge/{version_id}/review", json={"decision": "PASS"},
                headers=_h(writer))
    client.post(f"/v1/knowledge/{version_id}/publish", json={"audience": "internal"},
                headers=_h(writer))

    answer = client.post(
        "/v1/knowledge/answer",
        json={"query": "A112420 的单价是多少？", "knowledge_scope": "internal_product",
              "language": "zh-CN", "max_evidence": 5},
        headers=_h(writer),
    )
    body_json = answer.json()
    assert body_json["status"] == "ANSWERED", body_json
    assert "39.00" in body_json["answer"], body_json
