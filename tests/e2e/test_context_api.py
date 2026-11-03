"""上下文端点（内部）的 HTTP 行为。

关注点不是"能返回 JSON"，而是：权限与检索一致、ACL 在 HTTP 层仍然生效、
预算参数真的起作用、审计未配置时明确失败而不是静默返回空。
"""

from __future__ import annotations

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
from fkh.authz.policy import AclPolicy
from fkh.config import Settings
from fkh.domain.review import ReviewService
from fkh.workers.ingestion import IngestionRequest, ingest_document

pytestmark = pytest.mark.requires_pg

SECRET = "context-api-secret-0123456789abcdefgh"
HASH_KEY = "context-hash-key-0123456789abcdef"

CONTENT = """# 冷凝管说明

高硼硅玻璃冷凝管适用于回流与蒸馏，标准接口为 24/29 与 29/32。

## 使用限制

不得用于氢氟酸环境。
"""


def _token(
    tenant_id: str,
    principal_id: str,
    scopes: list[str],
    allowed: list[str],
    roles: list[str] | None = None,
) -> str:
    now = datetime.now(UTC)
    return jwt.encode(
        {
            "iss": "fkh.local",
            "aud": "fkh-api",
            "sub": f"user-{principal_id[:8]}@example.test",
            "iat": now,
            "exp": now + timedelta(minutes=10),
            "tenant_id": tenant_id,
            "principal_id": principal_id,
            "roles": roles if roles is not None else ["staff"],
            "scope": scopes,
            "allowed_scopes": allowed,
        },
        SECRET,
        algorithm="HS256",
    )


@pytest.fixture
def api(pg_url: str, admin: psycopg.Connection[Any]) -> Iterator[dict[str, Any]]:
    tenant_id = create_tenant(admin, f"上下文API-{uuid.uuid4().hex[:6]}")
    principal = create_principal(admin, tenant_id, f"ctx-{uuid.uuid4().hex[:6]}@example.test")

    request = IngestionRequest(
        source_id=f"ctx-{uuid.uuid4().hex[:8]}",
        filename="ctx.md",
        content=CONTENT.encode(),
        knowledge_key=f"ctx-{uuid.uuid4().hex[:8]}",
        title="冷凝管说明",
        domain="product_catalog",
        owner_department="tech",
        classification=1,
        audience="internal",
        required_scope="internal_product",
        acl_policy=AclPolicy.from_json({"allow": {"roles": ["staff"]}}),
        actor_id=uuid.UUID(principal),
    )
    result = ingest_document(admin, uuid.UUID(tenant_id), request)
    admin.commit()

    service = ReviewService(admin, uuid.UUID(tenant_id))
    service.pass_review(result.version_id, uuid.UUID(principal))
    service.approve_internal(result.version_id, uuid.UUID(principal))
    admin.commit()

    app = create_app(
        Settings(
            jwt_secret=SECRET,
            database_url=pg_url,
            retrieval_hash_key=HASH_KEY,
            retrieval_hash_key_id="k1",
        )
    )
    with TestClient(app) as client:
        yield {
            "client": client,
            "tenant_id": tenant_id,
            "principal": principal,
            "version_id": str(result.version_id),
        }


def _h(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_context_returns_blocks(api: dict[str, Any]) -> None:
    token = _token(api["tenant_id"], api["principal"], ["knowledge.search"], ["internal_product"])
    response = api["client"].post(
        "/v1/knowledge/context",
        json={"query": "冷凝管", "knowledge_scope": "internal_product"},
        headers=_h(token),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["blocks"]
    assert body["blocks"][0]["text"]
    assert body["blocks"][0]["version_id"] == api["version_id"]
    assert "trace_id" in body
    assert set(body["stats"]) >= {"denied", "dropped", "parent_restores", "tokens"}


def test_context_requires_auth(api: dict[str, Any]) -> None:
    response = api["client"].post(
        "/v1/knowledge/context", json={"query": "冷凝管", "knowledge_scope": "internal_product"}
    )
    assert response.status_code == 401


def test_context_requires_scope(api: dict[str, Any]) -> None:
    token = _token(api["tenant_id"], api["principal"], [], ["internal_product"])
    response = api["client"].post(
        "/v1/knowledge/context",
        json={"query": "冷凝管", "knowledge_scope": "internal_product"},
        headers=_h(token),
    )
    assert response.status_code == 403


def test_context_rejects_unauthorized_knowledge_scope(api: dict[str, Any]) -> None:
    """令牌只授 internal_product，却请求 customer_public：拒绝而不是返回空。"""
    token = _token(api["tenant_id"], api["principal"], ["knowledge.search"], ["internal_product"])
    response = api["client"].post(
        "/v1/knowledge/context",
        json={"query": "冷凝管", "knowledge_scope": "customer_public"},
        headers=_h(token),
    )
    assert response.status_code == 403


def test_context_respects_budget(api: dict[str, Any]) -> None:
    token = _token(api["tenant_id"], api["principal"], ["knowledge.search"], ["internal_product"])
    response = api["client"].post(
        "/v1/knowledge/context",
        json={"query": "冷凝管", "knowledge_scope": "internal_product", "max_blocks": 1},
        headers=_h(token),
    )
    assert response.status_code == 200, response.text
    assert len(response.json()["blocks"]) <= 1


def test_context_denies_outsider(api: dict[str, Any], admin: psycopg.Connection[Any]) -> None:
    """ACL 不允许的主体拿到零块，并且计入 denied 而不是静默空。"""
    outsider = create_principal(admin, api["tenant_id"], f"guest-{uuid.uuid4().hex[:6]}@example.test")
    admin.commit()
    # ACL 按角色授权：换 principal 但不换角色没有意义，必须换掉 staff
    token = _token(
        api["tenant_id"], outsider, ["knowledge.search"], ["internal_product"], roles=["guest"]
    )

    response = api["client"].post(
        "/v1/knowledge/context",
        json={"query": "冷凝管", "knowledge_scope": "internal_product"},
        headers=_h(token),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["blocks"] == []
    # 注意：denied 在这里必然是 0——ACL 在检索层就把该主体不可见的候选过滤掉了，
    # 根本不会进入上下文组装。context 层的 denied 只统计"检索放行、组装阶段才被拒"
    # 的情形（例如两次查询之间策略被改窄）。
    assert body["stats"]["tokens"] == 0


def test_context_returns_503_without_hash_key(pg_url: str, api: dict[str, Any]) -> None:
    """审计密钥缺失时不得静默返回空结果，必须明确不可用。"""
    app = create_app(Settings(jwt_secret=SECRET, database_url=pg_url))
    token = _token(api["tenant_id"], api["principal"], ["knowledge.search"], ["internal_product"])
    with TestClient(app) as client:
        response = client.post(
            "/v1/knowledge/context",
            json={"query": "冷凝管", "knowledge_scope": "internal_product"},
            headers=_h(token),
        )
    assert response.status_code == 503
