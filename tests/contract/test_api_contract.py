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

from fkh.api.main import create_app
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


HTTP_METHODS = {"get", "post", "put", "patch", "delete"}


def _registered_v1() -> set[tuple[str, str]]:
    """从应用实际暴露的 OpenAPI 取 v1 端点。

    不用 app.routes 遍历：FastAPI 会把 include_router 的路由折叠成懒加载对象，
    app.routes 里看不到子路由，据此断言会漏掉整块已实现的端点。
    动态读取而不是维护常量清单——手工清单迟早会忘记同步。
    """
    app = create_app(Settings(jwt_secret=SECRET))
    return {
        (method.upper(), path)
        for path, operations in app.openapi()["paths"].items()
        if path.startswith("/v1/")
        for method in operations
        if method.lower() in HTTP_METHODS
    }


def _declared_v1(spec: dict[str, Any]) -> set[tuple[str, str]]:
    pairs: set[tuple[str, str]] = set()
    for path, operations in spec["paths"].items():
        if not path.startswith("/v1/"):
            continue
        for method in operations:
            if method.lower() in HTTP_METHODS:
                pairs.add((method.upper(), path))
    return pairs


def test_openapi_matches_registered_routes() -> None:
    spec = yaml.safe_load(OPENAPI.read_text(encoding="utf-8"))
    registered = _registered_v1()
    declared = _declared_v1(spec)

    assert registered - declared == set(), f"代码有实现但契约未声明：{registered - declared}"
    assert declared - registered == set(), f"契约有声明但代码未实现：{declared - registered}"


def test_business_routes_declare_auth_and_failure_modes() -> None:
    spec = yaml.safe_load(OPENAPI.read_text(encoding="utf-8"))
    for method, path in _registered_v1():
        responses = spec["paths"][path][method.lower()]["responses"]
        assert "401" in responses, f"{method} {path} 未声明 401"
        # 已实现端点声明 200/201，未实现端点声明 501；两者必须至少有一个
        assert {"200", "201", "501"} & set(responses), f"{method} {path} 未声明成功或未实现响应"
