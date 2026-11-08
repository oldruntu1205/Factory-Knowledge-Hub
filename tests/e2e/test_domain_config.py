"""实例级知识域配置（ADR-009 + 0035）的真实 PostgreSQL 验收。

覆盖 L1 2026-11-08 有条件通过时点出的全部契约：
1. 已有租户迁移后带工厂 8 域；**新租户域清单为空**（不隐式补建工厂域）；
2. 未登记域导入 → 422，且提示可操作（`POST /v1/config/domains`）；
3. 登记幂等：首次 201 / 相同重试 200 / 同 code 不同 label 409（不静默改名）；
4. 库层复合外键守住"只能使用本租户已登记的域"；
5. `POST` 需要 `knowledge.write`；只有读权限 → 403；
6. 域**不参与授权**：登记/启用/传域过滤都不授予访问权（不可见文档仍不可见）；
7. 域是租户级的：A 登记的域在 B 不可用。
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

SECRET = "domain-config-secret-0123456789"
HASH_KEY = "domain-config-hmac-0123456789"
HASH_KEY_ID = "k-domain"

TEXT = "第 1 页\n\n货号 B220310 5kg 5L 220V 12800.00\n"


def _token(tenant: str, principal: str, scopes: tuple[str, ...]) -> str:
    now = datetime.now(UTC)
    return jwt.encode(
        {
            "iss": "fkh.local", "aud": "fkh-api", "sub": f"{principal[:8]}@domain.test",
            "iat": now, "exp": now + timedelta(minutes=20), "tenant_id": tenant,
            "principal_id": principal, "roles": ["staff"], "scope": list(scopes),
            "allowed_scopes": ["internal_product", "customer_public"],
        },
        SECRET,
        algorithm="HS256",
    )


@pytest.fixture(scope="module")
def role(pg_url: str) -> Iterator[str]:
    name, dsn = ensure_business_role(pg_url, prefix="fkh_domain")
    yield dsn
    drop_business_role(pg_url, name)


@pytest.fixture
def client(role: str) -> Iterator[TestClient]:
    settings = Settings(jwt_secret=SECRET, database_url=role,
                        retrieval_hash_key=HASH_KEY, retrieval_hash_key_id=HASH_KEY_ID)
    with TestClient(create_app(settings), raise_server_exceptions=False) as test_client:
        yield test_client


def _h(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


WRITER_SCOPES = ("knowledge.read", "knowledge.write", "knowledge.ingest",
                 "knowledge.review", "knowledge.publish", "knowledge.answer",
                 "knowledge.search", "evidence.read")


@pytest.fixture
def world(client: TestClient, admin: psycopg.Connection[Any]) -> dict[str, Any]:
    tenant = create_tenant(admin, f"DOMAIN {uuid.uuid4().hex[:6]}")
    ops = create_principal(admin, tenant, f"ops-{uuid.uuid4().hex[:6]}@domain.test")
    admin.commit()
    return {
        "tenant": tenant,
        "ops": ops,
        "writer": _token(tenant, ops, WRITER_SCOPES),
        "reader": _token(tenant, ops, ("knowledge.read", "knowledge.answer",
                                      "knowledge.search", "evidence.read")),
    }


def _upload(client: TestClient, token: str, domain: str, **overrides: Any) -> Any:
    body = {
        "filename": "doc.md", "title": "文档", "domain": domain,
        "owner_department": "ops", "classification": 1, "audience": "internal",
        "required_scope": "internal_product", "acl_allow_roles": ["staff"],
        "knowledge_key": f"dom-{uuid.uuid4().hex[:8]}",
        "content_base64": base64.b64encode(TEXT.encode()).decode(),
    }
    body.update(overrides)
    return client.post("/v1/knowledge/documents", json=body, headers=_h(token))


def test_new_tenant_starts_without_factory_domains(
    client: TestClient, admin: psycopg.Connection[Any]
) -> None:
    """新租户域清单为空：工厂 8 域是"模板预置"，不是运行时隐式补建。"""
    bare = create_tenant(admin, f"BARE {uuid.uuid4().hex[:6]}", seed_domains=False)
    principal = create_principal(admin, bare, f"bare-{uuid.uuid4().hex[:6]}@domain.test")
    admin.commit()
    token = _token(bare, principal, WRITER_SCOPES)

    listed = client.get("/v1/config/domains", headers=_h(token))
    assert listed.status_code == 200, listed.text
    assert listed.json()["domains"] == []

    rejected = _upload(client, token, "product_catalog")
    assert rejected.status_code == 422, rejected.text
    assert "config/domains" in rejected.json()["error"]["message"]

    created = client.post("/v1/config/domains",
                          json={"code": "product_catalog", "label": "产品目录"},
                          headers=_h(token))
    assert created.status_code == 201, created.text

    accepted = _upload(client, token, "product_catalog")
    assert accepted.status_code == 201, accepted.text


def test_existing_tenant_keeps_factory_domains(
    client: TestClient, world: dict[str, Any]
) -> None:
    listed = client.get("/v1/config/domains", headers=_h(world["writer"]))
    assert listed.status_code == 200, listed.text
    codes = {item["code"] for item in listed.json()["domains"]}
    assert codes == {
        "owner_experience", "glass_materials", "production_process", "product_catalog",
        "quality", "equipment_safety", "foreign_trade", "training",
    }, codes
    assert all(item["is_default"] for item in listed.json()["domains"])


def test_register_is_idempotent_and_rejects_rename(
    client: TestClient, world: dict[str, Any]
) -> None:
    first = client.post("/v1/config/domains",
                        json={"code": "bakery_equipment", "label": "烘焙设备"},
                        headers=_h(world["writer"]))
    assert first.status_code == 201, first.text
    assert first.json() == {"code": "bakery_equipment", "label": "烘焙设备",
                            "outcome": "CREATED", "created": True}

    again = client.post("/v1/config/domains",
                        json={"code": "bakery_equipment", "label": "烘焙设备"},
                        headers=_h(world["writer"]))
    assert again.status_code == 201, again.text
    assert again.json()["outcome"] == "EXISTS"
    assert again.json()["created"] is False

    rename = client.post("/v1/config/domains",
                         json={"code": "bakery_equipment", "label": "改名尝试"},
                         headers=_h(world["writer"]))
    assert rename.status_code == 409, rename.text
    assert rename.json()["error"]["code"] == "VERSION_CONFLICT"


def test_register_requires_write_scope(
    client: TestClient, world: dict[str, Any]
) -> None:
    denied = client.post("/v1/config/domains",
                         json={"code": "readonly_domain", "label": "只读试登记"},
                         headers=_h(world["reader"]))
    assert denied.status_code == 403, denied.text
    # 读权限仍可列清单
    assert client.get("/v1/config/domains", headers=_h(world["reader"])).status_code == 200


def test_database_enforces_registered_domains(
    client: TestClient, world: dict[str, Any], admin: psycopg.Connection[Any]
) -> None:
    with psycopg.connect(admin.info.dsn) as conn:
        conn.execute("SELECT set_config('app.tenant_id', %s, false)", (str(world["tenant"]),))
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            conn.execute(
                "INSERT INTO knowledge_documents"
                " (tenant_id, knowledge_key, domain, owner_department)"
                " VALUES (%s, %s, 'never_registered', 'ops')",
                (world["tenant"], f"fk-{uuid.uuid4().hex[:6]}"),
            )
        conn.rollback()


def test_domain_does_not_grant_visibility(
    client: TestClient, world: dict[str, Any], admin: psycopg.Connection[Any]
) -> None:
    """域只是分类标签：登记/启用域不会让本该不可见的文档变得可见。"""
    assert client.post("/v1/config/domains",
                       json={"code": "equipment_safety", "label": "设备与安全"},
                       headers=_h(world["writer"])).status_code in {201}

    uploaded = _upload(client, world["writer"], "equipment_safety",
                       audience="internal", required_scope="internal_product")
    assert uploaded.status_code == 201, uploaded.text
    version_id = uploaded.json()["version_id"]
    assert client.post(f"/v1/knowledge/{version_id}/review", json={"decision": "PASS"},
                       headers=_h(world["writer"])).status_code == 200
    assert client.post(f"/v1/knowledge/{version_id}/publish", json={"audience": "internal"},
                       headers=_h(world["writer"])).status_code == 200

    # 外部客户身份（无 internal_product 范围）即便传同一域过滤也检索不到
    customer = _token(world["tenant"], world["ops"], ("knowledge.answer", "knowledge.search"))
    found = client.post(
        "/v1/knowledge/search",
        json={"query": "B220310", "knowledge_scope": "customer_public", "max_results": 5},
        headers=_h(customer),
    )
    assert found.status_code == 200, found.text
    assert all(hit["version_id"] != version_id for hit in found.json()["results"])


def test_domain_is_tenant_scoped(
    client: TestClient, world: dict[str, Any], admin: psycopg.Connection[Any]
) -> None:
    other = create_tenant(admin, f"OTHER {uuid.uuid4().hex[:6]}")
    other_principal = create_principal(admin, other, f"o-{uuid.uuid4().hex[:6]}@domain.test")
    admin.commit()
    other_token = _token(other, other_principal, WRITER_SCOPES)

    assert client.post("/v1/config/domains",
                       json={"code": "hvac_maintenance", "label": "暖通维护"},
                       headers=_h(world["writer"])).status_code == 201

    # 另一租户看不到，也不能用
    listed = client.get("/v1/config/domains", headers=_h(other_token))
    assert listed.status_code == 200
    assert "hvac_maintenance" not in {i["code"] for i in listed.json()["domains"]}
    rejected = _upload(client, other_token, "hvac_maintenance")
    assert rejected.status_code == 422, rejected.text
