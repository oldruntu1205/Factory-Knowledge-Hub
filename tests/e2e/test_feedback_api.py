"""`POST /v1/feedback`：坏例登记只落脱敏引用，不落原始 query。

验收：
- 无 `feedback.create` → 403；
- trace 不存在 / 跨租户 → 404（不泄露存在性）；
- 成功 → 201，`bad_cases` 行关联真实 query_id，redacted_case 不含明文 query；
- 请求体额外字段 → 422。
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
from fkh.config import Settings

pytestmark = pytest.mark.requires_pg

SECRET = "feedback-secret-0123456789abcdef"


def _token(tenant: str, principal: str, scopes: tuple[str, ...]) -> str:
    now = datetime.now(UTC)
    payload = {
        "iss": "fkh.local",
        "aud": "fkh-api",
        "sub": f"{principal[:8]}@example.test",
        "iat": now,
        "exp": now + timedelta(minutes=10),
        "tenant_id": tenant,
        "principal_id": principal,
        "roles": ["staff"],
        "scope": list(scopes),
        "allowed_scopes": ["internal_product"],
    }
    return jwt.encode(payload, SECRET, algorithm="HS256")


@pytest.fixture
def client(pg_url: str) -> Iterator[TestClient]:
    with TestClient(create_app(Settings(jwt_secret=SECRET, database_url=pg_url))) as test_client:
        yield test_client


def _seed_trace(admin: psycopg.Connection[Any], tenant: str, principal: str) -> tuple[str, str]:
    trace_id = f"trace-{uuid.uuid4().hex[:12]}"
    query_id = uuid.uuid4()
    admin.execute(
        """INSERT INTO rag_queries
             (id, tenant_id, principal_id, query_hash, scope, status, trace_id)
           VALUES (%s, %s, %s, %s, 'internal_product', 'ANSWERED', %s)""",
        (query_id, tenant, principal, "hmac-of-query", trace_id),
    )
    return str(query_id), trace_id


def _h(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_feedback_requires_scope(
    client: TestClient, admin: psycopg.Connection[Any]
) -> None:
    tenant = create_tenant(admin, f"fb-no-scope-{uuid.uuid4().hex[:6]}")
    principal = create_principal(admin, tenant, "fb@example.test")
    admin.commit()
    token = _token(tenant, principal, scopes=("knowledge.read",))

    response = client.post(
        "/v1/feedback",
        json={"trace_id": "missing", "category": "WRONG_ANSWER", "expected_behavior": {"expected_status": "REFUSED"}},
        headers=_h(token),
    )
    assert response.status_code == 403, response.text


def test_feedback_unknown_trace_is_404(
    client: TestClient, admin: psycopg.Connection[Any]
) -> None:
    tenant = create_tenant(admin, f"fb-404-{uuid.uuid4().hex[:6]}")
    principal = create_principal(admin, tenant, "fb@example.test")
    admin.commit()
    token = _token(tenant, principal, scopes=("feedback.create",))

    response = client.post(
        "/v1/feedback",
        json={"trace_id": "does-not-exist", "category": "WRONG_ANSWER", "expected_behavior": {"expected_status": "REFUSED"}},
        headers=_h(token),
    )
    assert response.status_code == 404, response.text
    assert "does-not-exist" not in response.text


def test_feedback_records_redacted_bad_case(
    client: TestClient, admin: psycopg.Connection[Any]
) -> None:
    tenant = create_tenant(admin, f"fb-ok-{uuid.uuid4().hex[:6]}")
    principal = create_principal(admin, tenant, "fb@example.test")
    query_id, trace_id = _seed_trace(admin, tenant, principal)
    admin.commit()
    token = _token(tenant, principal, scopes=("feedback.create",))

    response = client.post(
        "/v1/feedback",
        json={
            "trace_id": trace_id,
            "category": "WRONG_ANSWER",
            "expected_behavior": {"expected_status": "REFUSED", "must_cite": False},
        },
        headers=_h(token),
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["trace_id"] == trace_id
    assert body["regression_status"] == "OPEN"

    row = admin.execute(
        """SELECT query_id, category, severity, redacted_case, expected_behavior
             FROM bad_cases WHERE tenant_id = %s AND id = %s""",
        (tenant, body["bad_case_id"]),
    ).fetchone()
    assert row is not None
    assert str(row[0]) == query_id
    assert row[1] == "WRONG_ANSWER"
    assert row[2] == "P2"
    assert set(row[3]) == {"trace_id", "query_hash", "category"}
    assert row[3]["query_hash"] == "hmac-of-query"
    assert "query" not in row[3]
    assert row[4] == {"expected_status": "REFUSED", "must_cite": False}


def test_feedback_cross_tenant_trace_is_404(
    client: TestClient, admin: psycopg.Connection[Any]
) -> None:
    tenant_a = create_tenant(admin, f"fb-a-{uuid.uuid4().hex[:6]}")
    tenant_b = create_tenant(admin, f"fb-b-{uuid.uuid4().hex[:6]}")
    principal_a = create_principal(admin, tenant_a, "fb-a@example.test")
    principal_b = create_principal(admin, tenant_b, "fb-b@example.test")
    _query_b, trace_b = _seed_trace(admin, tenant_b, principal_b)
    admin.commit()

    response = client.post(
        "/v1/feedback",
        json={"trace_id": trace_b, "category": "WRONG_ANSWER", "expected_behavior": {"expected_status": "REFUSED"}},
        headers=_h(_token(tenant_a, principal_a, scopes=("feedback.create",))),
    )
    assert response.status_code == 404, response.text
    assert trace_b not in response.text


def test_feedback_rejects_extra_fields(
    client: TestClient, admin: psycopg.Connection[Any]
) -> None:
    tenant = create_tenant(admin, f"fb-422-{uuid.uuid4().hex[:6]}")
    principal = create_principal(admin, tenant, "fb@example.test")
    admin.commit()

    response = client.post(
        "/v1/feedback",
        json={
            "trace_id": "x",
            "category": "WRONG_ANSWER",
            "expected_behavior": {"expected_status": "REFUSED"},
            "tenant_id": "attacker",
        },
        headers=_h(_token(tenant, principal, scopes=("feedback.create",))),
    )
    assert response.status_code == 422, response.text


def test_feedback_rejects_free_text_expected_behavior(
    client: TestClient, admin: psycopg.Connection[Any]
) -> None:
    tenant = create_tenant(admin, f"fb-free-{uuid.uuid4().hex[:6]}")
    principal = create_principal(admin, tenant, "fb@example.test")
    admin.commit()

    response = client.post(
        "/v1/feedback",
        json={
            "trace_id": "x",
            "category": "WRONG_ANSWER",
            "expected_behavior": {"expected_status": "REFUSED", "notes": "原始问题：..."},
        },
        headers=_h(_token(tenant, principal, scopes=("feedback.create",))),
    )
    assert response.status_code == 422, response.text


def test_feedback_rejects_oversized_body(
    client: TestClient, admin: psycopg.Connection[Any]
) -> None:
    tenant = create_tenant(admin, f"fb-big-{uuid.uuid4().hex[:6]}")
    principal = create_principal(admin, tenant, "fb@example.test")
    admin.commit()

    response = client.post(
        "/v1/feedback",
        content=b"x" * (16 * 1024 + 1),
        headers={
            **_h(_token(tenant, principal, scopes=("feedback.create",))),
            "Content-Type": "application/json",
        },
    )
    assert response.status_code == 413, response.text


def test_feedback_rejects_chunked_oversized_body(
    client: TestClient, admin: psycopg.Connection[Any]
) -> None:
    """没有 Content-Length 的 chunked 请求也不能绕过累计上限。"""
    tenant = create_tenant(admin, f"fb-chunk-{uuid.uuid4().hex[:6]}")
    principal = create_principal(admin, tenant, "fb@example.test")
    admin.commit()

    def chunks() -> Any:
        yield b'{"trace_id":"x","category":"X","expected_behavior":{"expected_status":"REFUSED"}}'
        yield b" " * (16 * 1024)

    response = client.post(
        "/v1/feedback",
        content=chunks(),
        headers={
            **_h(_token(tenant, principal, scopes=("feedback.create",))),
            "Content-Type": "application/json",
        },
    )
    assert response.status_code == 413, response.text


def test_feedback_exactly_16k_is_not_rejected_by_limit(
    client: TestClient, admin: psycopg.Connection[Any]
) -> None:
    tenant = create_tenant(admin, f"fb-edge-{uuid.uuid4().hex[:6]}")
    principal = create_principal(admin, tenant, "fb@example.test")
    admin.commit()

    payload = (
        b'{"trace_id":"edge","category":"WRONG_ANSWER",'
        b'"expected_behavior":{"expected_status":"REFUSED"}}'
    )
    body = payload + b" " * (16 * 1024 - len(payload))
    assert len(body) == 16 * 1024

    response = client.post(
        "/v1/feedback",
        content=body,
        headers={
            **_h(_token(tenant, principal, scopes=("feedback.create",))),
            "Content-Type": "application/json",
        },
    )
    # 限额内；trace 不存在 → 404，而不是 413
    assert response.status_code == 404, response.text
