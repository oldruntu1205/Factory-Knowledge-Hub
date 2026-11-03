"""API 行为与 OpenAPI 契约一致性。

重点不是"接口能返回 200"，而是：
- 未认证 / 无权限 / 未实现三种状态各自返回正确的错误结构与状态码；
- 代码里注册的路由与 openapi.yaml 声明的路径集合完全一致，不漏不虚。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import jwt
import pytest
import yaml
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator

from fkh.api.main import CONTRACT_ROUTES, create_app
from fkh.config import Settings

pytestmark = [pytest.mark.contract]

ROOT = Path(__file__).resolve().parents[2]
OPENAPI = ROOT / "contracts" / "openapi" / "openapi.yaml"
ERROR_SCHEMA = ROOT / "contracts" / "schema" / "error.json"

SECRET = "phase0-test-secret-0123456789abcdefghij"


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(create_app(Settings(jwt_secret=SECRET)))


def _token(*, scopes: list[str] | None = None, allowed: list[str] | None = None, expired: bool = False) -> str:
    now = datetime.now(UTC)
    payload: dict[str, Any] = {
        "iss": "fkh.local",
        "aud": "fkh-api",
        "sub": "user-a@example.test",
        "iat": now,
        "exp": now + timedelta(minutes=-5 if expired else 5),
        "tenant_id": str(uuid.uuid4()),
        "principal_id": str(uuid.uuid4()),
        "roles": ["staff"],
        "scope": scopes if scopes is not None else ["knowledge.answer"],
        "allowed_scopes": allowed if allowed is not None else ["internal_product"],
    }
    return jwt.encode(payload, SECRET, algorithm="HS256")


def _assert_error(response: Any, code: str) -> None:
    body = response.json()
    schema = __import__("json").loads(ERROR_SCHEMA.read_text(encoding="utf-8"))
    Draft202012Validator(schema).validate(body)
    assert body["error"]["code"] == code


def test_live_probe(client: TestClient) -> None:
    response = client.get("/health/live")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_ready_reports_degraded_without_database(client: TestClient) -> None:
    """未配置数据库时必须 503，不能让编排层把流量导进来。"""
    response = client.get("/health/ready")
    assert response.status_code == 503
    assert response.json()["checks"]["database"] == "not_configured"


def test_answer_requires_token(client: TestClient) -> None:
    response = client.post(
        "/v1/knowledge/answer",
        json={"query": "冷凝管规格", "knowledge_scope": "internal_product", "language": "zh-CN"},
    )
    assert response.status_code == 401
    _assert_error(response, "UNAUTHENTICATED")


def test_expired_token_rejected(client: TestClient) -> None:
    response = client.post(
        "/v1/knowledge/answer",
        json={"query": "冷凝管规格", "knowledge_scope": "internal_product", "language": "zh-CN"},
        headers={"Authorization": f"Bearer {_token(expired=True)}"},
    )
    assert response.status_code == 401
    _assert_error(response, "UNAUTHENTICATED")


def test_scope_escalation_rejected(client: TestClient) -> None:
    """令牌只授予 internal_product，却请求 customer_public，必须 403 而不是降级放行。"""
    response = client.post(
        "/v1/knowledge/answer",
        json={"query": "价格多少", "knowledge_scope": "customer_public", "language": "zh-CN"},
        headers={"Authorization": f"Bearer {_token(allowed=['internal_product'])}"},
    )
    assert response.status_code == 403
    _assert_error(response, "ACCESS_DENIED")


def test_missing_permission_rejected(client: TestClient) -> None:
    response = client.post(
        "/v1/knowledge/answer",
        json={"query": "冷凝管规格", "knowledge_scope": "internal_product", "language": "zh-CN"},
        headers={"Authorization": f"Bearer {_token(scopes=[])}"},
    )
    assert response.status_code == 403


def test_answer_invalid_body_rejected_before_implementation(client: TestClient) -> None:
    """请求校验必须发生在"未实现"之前，否则契约校验形同虚设。"""
    response = client.post(
        "/v1/knowledge/answer",
        json={"query": "   ", "knowledge_scope": "internal_product", "language": "zh-CN"},
        headers={"Authorization": f"Bearer {_token()}"},
    )
    assert response.status_code == 422


def test_answer_returns_501_when_authorized(client: TestClient) -> None:
    response = client.post(
        "/v1/knowledge/answer",
        json={"query": "冷凝管规格", "knowledge_scope": "internal_product", "language": "zh-CN"},
        headers={"Authorization": f"Bearer {_token()}"},
    )
    assert response.status_code == 501
    _assert_error(response, "NOT_IMPLEMENTED")


def test_trace_header_present(client: TestClient) -> None:
    response = client.get("/health/live")
    assert response.headers.get("X-Trace-Id")
    assert response.headers["X-Trace-Id"]


def test_trace_header_echoed(client: TestClient) -> None:
    response = client.get("/health/live", headers={"X-Trace-Id": "trace-fixed-1"})
    assert response.headers["X-Trace-Id"] == "trace-fixed-1"


def test_openapi_matches_registered_routes() -> None:
    spec = yaml.safe_load(OPENAPI.read_text(encoding="utf-8"))
    declared = spec["paths"]
    expected = {(method, path) for method, path, _scope, _summary in CONTRACT_ROUTES}
    expected |= {("POST", "/v1/knowledge/answer"), ("POST", "/v1/training/ask")}

    for method, path in expected:
        assert path in declared, f"openapi.yaml 缺少路径 {path}"
        assert method.lower() in declared[path], f"{path} 缺少方法 {method}"

    business = {
        (method, path)
        for method, path in expected
        if path.startswith("/v1/")
    }
    for method, path in business:
        responses = declared[path][method.lower()]["responses"]
        assert "401" in responses, f"{path} 未声明 401"
        assert "501" in responses, f"{path} 未声明 501（Phase 0 应显式标注未实现）"


def test_openapi_has_no_undocumented_v1_route() -> None:
    spec = yaml.safe_load(OPENAPI.read_text(encoding="utf-8"))
    declared_v1 = {path for path in spec["paths"] if path.startswith("/v1/")}
    registered_v1 = {path for _m, path, _s, _sum in CONTRACT_ROUTES} | {
        "/v1/knowledge/answer",
        "/v1/training/ask",
    }
    assert declared_v1 == registered_v1
