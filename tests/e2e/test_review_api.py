"""审核 API 端到端：HTTP 层 + 真实数据库 + 真实 JWT。

服务层测试已验证状态机与双签规则；这里验证的是 HTTP 边界：
权限从令牌来、错误码不泄露资源存在性、连接池与事务边界正确。
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
from fkh.workers.ingestion import IngestionRequest, ingest_document

pytestmark = pytest.mark.requires_pg

SECRET = "review-api-secret-0123456789abcdefghij"
CONTENT = """# 冷凝管公开说明

高硼硅玻璃冷凝管适用于回流与蒸馏，标准接口为 24/29 与 29/32。

长度可选 300 / 400 / 500 mm。价格与交期需业务确认。
"""


def _token(tenant_id: str, principal_id: str, scopes: list[str]) -> str:
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
        "scope": scopes,
        "allowed_scopes": ["internal_product"],
    }
    return jwt.encode(payload, SECRET, algorithm="HS256")


@pytest.fixture
def api(pg_url: str, admin: psycopg.Connection[Any]) -> Iterator[dict[str, Any]]:
    tenant_id = create_tenant(admin, f"API 租户 {uuid.uuid4().hex[:6]}")
    reviewer = create_principal(admin, tenant_id, f"reviewer-{uuid.uuid4().hex[:6]}@example.test")
    publisher = create_principal(admin, tenant_id, f"publisher-{uuid.uuid4().hex[:6]}@example.test")

    request = IngestionRequest(
        source_id=f"src-{uuid.uuid4().hex[:8]}",
        filename="public-note.md",
        content=CONTENT.encode(),
        knowledge_key=f"api-{uuid.uuid4().hex[:8]}",
        title="冷凝管公开说明",
        domain="product_catalog",
        owner_department="tech",
        classification=0,
        audience="customer_public",
        acl_policy=AclPolicy.from_json({"allow": {"roles": ["staff"]}}),
        actor_id=uuid.UUID(reviewer),
    )
    result = ingest_document(admin, uuid.UUID(tenant_id), request)
    admin.commit()

    app = create_app(Settings(jwt_secret=SECRET, database_url=pg_url))
    with TestClient(app) as client:
        yield {
            "client": client,
            "tenant_id": tenant_id,
            "reviewer": reviewer,
            "publisher": publisher,
            "version_id": str(result.version_id),
        }


def _h(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_unauthenticated_rejected(api: dict[str, Any]) -> None:
    response = api["client"].post(
        f"/v1/knowledge/{api['version_id']}/review", json={"decision": "PASS"}
    )
    assert response.status_code == 401


def test_missing_scope_rejected(api: dict[str, Any]) -> None:
    token = _token(api["tenant_id"], api["reviewer"], ["knowledge.answer"])
    response = api["client"].post(
        f"/v1/knowledge/{api['version_id']}/review",
        json={"decision": "PASS"},
        headers=_h(token),
    )
    assert response.status_code == 403


def test_full_review_and_publish(api: dict[str, Any]) -> None:
    client = api["client"]
    vid = api["version_id"]
    reviewer_token = _token(api["tenant_id"], api["reviewer"], ["knowledge.review", "knowledge.publish"])
    publisher_token = _token(
        api["tenant_id"], api["publisher"], ["knowledge.publish", "knowledge.publish_public"]
    )

    step = client.post(
        f"/v1/knowledge/{vid}/review", json={"decision": "PASS"}, headers=_h(reviewer_token)
    )
    assert step.status_code == 200, step.text
    assert step.json()["status"] == "REVIEWED"

    step = client.post(
        f"/v1/knowledge/{vid}/review", json={"decision": "APPROVE_INTERNAL"}, headers=_h(reviewer_token)
    )
    assert step.status_code == 200, step.text
    assert step.json()["status"] == "APPROVED_INTERNAL"

    step = client.post(
        f"/v1/knowledge/{vid}/publish", json={"audience": "internal"}, headers=_h(reviewer_token)
    )
    assert step.status_code == 200, step.text
    assert step.json()["status"] == "APPROVED_INTERNAL"

    step = client.post(
        f"/v1/knowledge/{vid}/publish",
        json={"audience": "customer_public"},
        headers=_h(publisher_token),
    )
    assert step.status_code == 200, step.text
    body = step.json()
    assert body["status"] == "PUBLISHED_PUBLIC"
    assert body["public_approved_by"] == api["publisher"]
    assert body["approved_by"] == api["reviewer"]


def test_publish_public_requires_extra_scope(api: dict[str, Any]) -> None:
    """只有 knowledge.publish 而没有 publish_public 时，不得对外发布。"""
    client = api["client"]
    vid = api["version_id"]
    token = _token(api["tenant_id"], api["reviewer"], ["knowledge.review", "knowledge.publish"])

    client.post(f"/v1/knowledge/{vid}/review", json={"decision": "PASS"}, headers=_h(token))
    client.post(
        f"/v1/knowledge/{vid}/review", json={"decision": "APPROVE_INTERNAL"}, headers=_h(token)
    )

    response = client.post(
        f"/v1/knowledge/{vid}/publish", json={"audience": "customer_public"}, headers=_h(token)
    )
    assert response.status_code == 403


def test_same_person_public_publish_conflicts(api: dict[str, Any]) -> None:
    client = api["client"]
    vid = api["version_id"]
    token = _token(
        api["tenant_id"],
        api["reviewer"],
        ["knowledge.review", "knowledge.publish", "knowledge.publish_public"],
    )

    client.post(f"/v1/knowledge/{vid}/review", json={"decision": "PASS"}, headers=_h(token))
    client.post(
        f"/v1/knowledge/{vid}/review", json={"decision": "APPROVE_INTERNAL"}, headers=_h(token)
    )

    response = client.post(
        f"/v1/knowledge/{vid}/publish", json={"audience": "customer_public"}, headers=_h(token)
    )
    assert response.status_code == 409


def test_revoke(api: dict[str, Any]) -> None:
    client = api["client"]
    vid = api["version_id"]
    token = _token(
        api["tenant_id"], api["reviewer"], ["knowledge.review", "knowledge.revoke"]
    )

    client.post(f"/v1/knowledge/{vid}/review", json={"decision": "PASS"}, headers=_h(token))
    # REVIEWED -> REVOKED 不在白名单内（同样属于"未批准内容无出口"，见审查单），
    # 所以先批准为内部可用再撤销
    client.post(
        f"/v1/knowledge/{vid}/review", json={"decision": "APPROVE_INTERNAL"}, headers=_h(token)
    )
    response = client.post(f"/v1/knowledge/{vid}/revoke?reason=内容有误", headers=_h(token))
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "REVOKED"


def test_unknown_version_does_not_leak_existence(api: dict[str, Any]) -> None:
    """不存在的版本与别人的版本必须返回同一个 403，不能出现 404。"""
    token = _token(api["tenant_id"], api["reviewer"], ["knowledge.review"])
    response = api["client"].post(
        f"/v1/knowledge/{uuid.uuid4()}/review", json={"decision": "PASS"}, headers=_h(token)
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "ACCESS_DENIED"


def test_invalid_uuid_is_validation_error(api: dict[str, Any]) -> None:
    token = _token(api["tenant_id"], api["reviewer"], ["knowledge.review"])
    response = api["client"].post(
        "/v1/knowledge/not-a-uuid/review", json={"decision": "PASS"}, headers=_h(token)
    )
    assert response.status_code == 422
