"""证据定位 API 端到端：HTTP 边界 + 真实数据库 + 真实 JWT。

逐条对应 2026-11-03 L1 判决「必须补的测试」1–8 条（_evidence/l1_reply_evidence_20261103.md）。
服务层规则（状态/组合/scope/ACL）在这里验证的是**从令牌到正文的完整链路**：
权限必须来自令牌、拒绝必须同码同 body、审计必须落库且不可跨租户读。
"""

from __future__ import annotations

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
    set_tenant,
    sha256_hex,
)

from fkh.api.main import create_app
from fkh.config import Settings

pytestmark = pytest.mark.requires_pg

SECRET = "evidence-api-secret-0123456789abcdefgh"
ROOT = Path(__file__).resolve().parents[2]
EVIDENCE_SCHEMA = json.loads(
    (ROOT / "contracts" / "schema" / "evidence_response.json").read_text(encoding="utf-8")
)


def _token(
    tenant_id: str,
    principal_id: str,
    *,
    scopes: tuple[str, ...] = ("evidence.read",),
    allowed: tuple[str, ...] = ("internal_product",),
    roles: tuple[str, ...] = ("staff",),
    groups: tuple[str, ...] = (),
    departments: tuple[str, ...] = (),
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
        "roles": list(roles),
        "groups": list(groups),
        "departments": list(departments),
        "scope": list(scopes),
        "allowed_scopes": list(allowed),
    }
    return jwt.encode(payload, SECRET, algorithm="HS256")


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def client(pg_url: str) -> Iterator[TestClient]:
    with TestClient(create_app(Settings(jwt_secret=SECRET, database_url=pg_url))) as test_client:
        yield test_client


@pytest.fixture
def tenant(admin: psycopg.Connection[Any]) -> str:
    tenant_id = create_tenant(admin, f"证据租户 {uuid.uuid4().hex[:6]}")
    admin.commit()
    return tenant_id


def _insert_chunk(
    admin: psycopg.Connection[Any],
    tenant_id: str,
    version_id: str,
    acl_id: str,
    *,
    kind: str = "child",
    parent_id: str | None = None,
    text: str = "示例证据正文",
    locator: dict[str, Any] | None = None,
) -> str:
    row = admin.execute(
        """INSERT INTO knowledge_chunks
             (tenant_id, version_id, parent_chunk_id, chunk_kind, chunk_text, chunk_sha256,
              tokenizer_id, heading_path, locator, acl_policy_id, index_generation)
           VALUES (%s, %s, %s, %s, %s, %s, 'bge-m3-zh-v1', ARRAY['第 1 章'],
                   %s::jsonb, %s, 1)
           RETURNING id""",
        (
            tenant_id,
            version_id,
            parent_id,
            kind,
            text,
            sha256_hex(text),
            psycopg.types.json.Jsonb(locator or {"block_index": 0, "page_start": 1}),
            acl_id,
        ),
    ).fetchone()
    assert row is not None
    return str(row[0])


def _seed(
    admin: psycopg.Connection[Any],
    tenant_id: str,
    *,
    audience: str = "internal",
    status: str = "APPROVED_INTERNAL",
    required_scope: str | None = "internal_product",
    classification: int = 0,
    parent_acl: dict[str, Any] | None = None,
    child_acls: list[dict[str, Any]] | None = None,
    locator: dict[str, Any] | None = None,
) -> dict[str, Any]:
    approver = create_principal(admin, tenant_id, f"approver-{uuid.uuid4().hex[:6]}@example.test")
    second = create_principal(admin, tenant_id, f"approver2-{uuid.uuid4().hex[:6]}@example.test")
    doc = create_document(admin, tenant_id, f"doc-{uuid.uuid4().hex[:8]}")
    version = create_version(
        admin,
        tenant_id,
        doc,
        status=status,
        classification=classification,
        audience=audience,
        approved_by=approver if status != "DRAFT" else None,
        public_approved_by=second if status == "PUBLISHED_PUBLIC" else None,
    )
    if required_scope is not None:
        admin.execute(
            "UPDATE knowledge_versions SET required_scope = %s WHERE id = %s",
            (required_scope, version),
        )

    parent_acl_id = create_acl(admin, tenant_id, parent_acl or {"allow": {"roles": ["staff"]}})
    parent_id = _insert_chunk(
        admin,
        tenant_id,
        version,
        parent_acl_id,
        kind="parent",
        text="父块全文：冷凝管规格与接口说明。",
        locator=locator or {"block_index": 0, "page_start": 1},
    )
    child_ids = [
        _insert_chunk(
            admin,
            tenant_id,
            version,
            create_acl(admin, tenant_id, acl),
            parent_id=parent_id,
            text=f"子块正文 {index}",
        )
        for index, acl in enumerate(child_acls or [{"allow": {"roles": ["staff"]}}])
    ]
    admin.commit()
    return {
        "document_id": doc,
        "version_id": version,
        "parent_id": parent_id,
        "child_ids": child_ids,
        "principal_id": approver,
    }


def _body(response: Any) -> dict[str, Any]:
    assert response.status_code in {200, 403}, response.text
    return response.json()


def _strip_trace(payload: dict[str, Any]) -> dict[str, Any]:
    copied = dict(payload)
    if "error" in copied:
        copied["error"] = {k: v for k, v in copied["error"].items() if k != "trace_id"}
    copied.pop("trace_id", None)
    return copied


# ---------------------------------------------------------------------------
# 1. 缺少 evidence.read
# ---------------------------------------------------------------------------
def test_missing_evidence_read_scope_is_denied(
    client: TestClient, admin: psycopg.Connection[Any], tenant: str
) -> None:
    seeded = _seed(admin, tenant)
    token = _token(tenant, seeded["principal_id"], scopes=())

    response = client.get(f"/v1/evidence/{seeded['child_ids'][0]}", headers=_auth(token))
    assert response.status_code == 403
    assert _body(response)["error"]["code"] == "ACCESS_DENIED"


# ---------------------------------------------------------------------------
# 2. 五种拒绝同码同 body
# ---------------------------------------------------------------------------
def test_all_denials_are_indistinguishable(
    client: TestClient, admin: psycopg.Connection[Any], tenant: str
) -> None:
    other_tenant = create_tenant(admin, f"对照租户 {uuid.uuid4().hex[:6]}")
    readable = _seed(admin, tenant)
    hidden = _seed(admin, tenant, child_acls=[{"allow": {"roles": ["manager"]}}])
    draft = _seed(admin, tenant, status="DRAFT", required_scope=None)
    training = _seed(admin, tenant, required_scope="internal_training")
    foreign = _seed(admin, other_tenant)
    admin.commit()

    base = _token(tenant, readable["principal_id"])
    attempts = [
        client.get(f"/v1/evidence/{uuid.uuid4()}", headers=_auth(base)),  # 不存在
        client.get(f"/v1/evidence/{foreign['child_ids'][0]}", headers=_auth(base)),  # 跨租户
        client.get(f"/v1/evidence/{draft['child_ids'][0]}", headers=_auth(base)),  # 状态不可见
        client.get(f"/v1/evidence/{hidden['child_ids'][0]}", headers=_auth(base)),  # ACL 拒绝
        client.get(f"/v1/evidence/{training['child_ids'][0]}", headers=_auth(base)),  # scope 不符
    ]
    bodies = []
    for response in attempts:
        assert response.status_code == 403
        body = _body(response)
        assert body["error"]["code"] == "ACCESS_DENIED"
        bodies.append(_strip_trace(body))

    assert all(body == bodies[0] for body in bodies)
    # 不得回带资源元数据（id / 标题 / 状态）
    serialized = json.dumps(bodies[0], ensure_ascii=False)
    assert foreign["child_ids"][0] not in serialized


# ---------------------------------------------------------------------------
# 3. deny 覆盖 allow；只有 groups 的 allow 不能靠未解析维度放行
# ---------------------------------------------------------------------------
def test_deny_overrides_allow(
    client: TestClient, admin: psycopg.Connection[Any], tenant: str
) -> None:
    seeded = _seed(
        admin,
        tenant,
        child_acls=[{"allow": {"roles": ["staff"]}, "deny": {"roles": ["staff"]}}],
    )
    token = _token(tenant, seeded["principal_id"])
    response = client.get(f"/v1/evidence/{seeded['child_ids'][0]}", headers=_auth(token))
    assert response.status_code == 403


def test_group_only_allow_does_not_grant_without_group_claim(
    client: TestClient, admin: psycopg.Connection[Any], tenant: str
) -> None:
    seeded = _seed(admin, tenant, child_acls=[{"allow": {"groups": ["tech"]}}])
    token = _token(tenant, seeded["principal_id"], groups=())
    assert client.get(
        f"/v1/evidence/{seeded['child_ids'][0]}", headers=_auth(token)
    ).status_code == 403

    with_group = _token(tenant, seeded["principal_id"], groups=("tech",))
    assert client.get(
        f"/v1/evidence/{seeded['child_ids'][0]}", headers=_auth(with_group)
    ).status_code == 200


# ---------------------------------------------------------------------------
# 4. 状态白名单与状态/audience 组合
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("status", ["DRAFT", "REVIEWED", "SUPERSEDED", "REVOKED"])
def test_non_whitelisted_status_cannot_read(
    client: TestClient, admin: psycopg.Connection[Any], tenant: str, status: str
) -> None:
    seeded = _seed(admin, tenant, status=status, required_scope=None)
    token = _token(tenant, seeded["principal_id"])
    assert client.get(
        f"/v1/evidence/{seeded['child_ids'][0]}", headers=_auth(token)
    ).status_code == 403


def test_status_audience_mismatch_is_denied(
    client: TestClient, admin: psycopg.Connection[Any], tenant: str
) -> None:
    # APPROVED_INTERNAL 只允许 internal；这里造一个 audience=customer_public 的错配组合
    seeded = _seed(
        admin,
        tenant,
        status="APPROVED_INTERNAL",
        audience="customer_public",
        required_scope="customer_public",
    )
    token = _token(tenant, seeded["principal_id"], allowed=("customer_public",))
    assert client.get(
        f"/v1/evidence/{seeded['child_ids'][0]}", headers=_auth(token)
    ).status_code == 403


# ---------------------------------------------------------------------------
# 5. required_scope 匹配
# ---------------------------------------------------------------------------
def test_required_scope_must_match_token(
    client: TestClient, admin: psycopg.Connection[Any], tenant: str
) -> None:
    seeded = _seed(admin, tenant, required_scope="internal_training")
    product_token = _token(tenant, seeded["principal_id"], allowed=("internal_product",))
    training_token = _token(tenant, seeded["principal_id"], allowed=("internal_training",))

    assert client.get(
        f"/v1/evidence/{seeded['child_ids'][0]}", headers=_auth(product_token)
    ).status_code == 403
    assert client.get(
        f"/v1/evidence/{seeded['child_ids'][0]}", headers=_auth(training_token)
    ).status_code == 200


def test_missing_required_scope_is_denied(
    client: TestClient, admin: psycopg.Connection[Any], tenant: str
) -> None:
    seeded = _seed(admin, tenant, required_scope=None)
    token = _token(tenant, seeded["principal_id"])
    assert client.get(
        f"/v1/evidence/{seeded['child_ids'][0]}", headers=_auth(token)
    ).status_code == 403


def test_published_public_requires_customer_public_scope(
    client: TestClient, admin: psycopg.Connection[Any], tenant: str
) -> None:
    seeded = _seed(
        admin,
        tenant,
        status="PUBLISHED_PUBLIC",
        audience="customer_public",
        required_scope="customer_public",
    )
    internal = _token(tenant, seeded["principal_id"], allowed=("internal_product",))
    public = _token(tenant, seeded["principal_id"], allowed=("customer_public",))

    assert client.get(
        f"/v1/evidence/{seeded['child_ids'][0]}", headers=_auth(internal)
    ).status_code == 403
    assert client.get(
        f"/v1/evidence/{seeded['child_ids'][0]}", headers=_auth(public)
    ).status_code == 200


# ---------------------------------------------------------------------------
# 6. 父块恢复与绕过
# ---------------------------------------------------------------------------
def test_child_returns_parent_text_when_every_child_is_readable(
    client: TestClient, admin: psycopg.Connection[Any], tenant: str
) -> None:
    seeded = _seed(admin, tenant)
    token = _token(tenant, seeded["principal_id"])

    response = client.get(f"/v1/evidence/{seeded['child_ids'][0]}", headers=_auth(token))
    assert response.status_code == 200
    body = _body(response)
    assert body["text_level"] == "PARENT"
    assert body["text_evidence_id"] == seeded["parent_id"]
    assert body["evidence_id"] == seeded["child_ids"][0]
    assert "父块全文" in body["text"]


def test_child_falls_back_when_a_sibling_is_denied(
    client: TestClient, admin: psycopg.Connection[Any], tenant: str
) -> None:
    seeded = _seed(
        admin,
        tenant,
        child_acls=[
            {"allow": {"roles": ["staff"]}},
            {"allow": {"roles": ["manager"]}},
        ],
    )
    token = _token(tenant, seeded["principal_id"])

    body = _body(client.get(f"/v1/evidence/{seeded['child_ids'][0]}", headers=_auth(token)))
    assert body["text_level"] == "CHILD"
    assert body["text_evidence_id"] == seeded["child_ids"][0]
    assert "父块全文" not in body["text"]


def test_direct_parent_request_cannot_bypass_child_checks(
    client: TestClient, admin: psycopg.Connection[Any], tenant: str
) -> None:
    seeded = _seed(
        admin,
        tenant,
        child_acls=[
            {"allow": {"roles": ["staff"]}},
            {"allow": {"roles": ["manager"]}},
        ],
    )
    token = _token(tenant, seeded["principal_id"])

    response = client.get(f"/v1/evidence/{seeded['parent_id']}", headers=_auth(token))
    assert response.status_code == 403


def test_direct_parent_request_allowed_when_all_children_readable(
    client: TestClient, admin: psycopg.Connection[Any], tenant: str
) -> None:
    seeded = _seed(admin, tenant)
    token = _token(tenant, seeded["principal_id"])

    body = _body(client.get(f"/v1/evidence/{seeded['parent_id']}", headers=_auth(token)))
    assert body["text_level"] == "PARENT"
    assert body["evidence_id"] == seeded["parent_id"]
    assert body["text_evidence_id"] == seeded["parent_id"]


# ---------------------------------------------------------------------------
# 7. 签名链接与路径泄露
# ---------------------------------------------------------------------------
def test_signed_url_not_configured_and_no_storage_leak(
    client: TestClient, admin: psycopg.Connection[Any], tenant: str
) -> None:
    seeded = _seed(
        admin,
        tenant,
        locator={
            "block_index": 2,
            "page_start": 7,
            "object_key": "s3://private-bucket/secret.pdf",
            "chunk_sha256": "f" * 64,
        },
    )
    token = _token(tenant, seeded["principal_id"])

    response = client.get(f"/v1/evidence/{seeded['child_ids'][0]}", headers=_auth(token))
    assert response.status_code == 200
    body = _body(response)
    Draft202012Validator(EVIDENCE_SCHEMA, format_checker=FormatChecker()).validate(body)

    assert body["signed_url"] is None
    assert body["signed_url_state"] == "NOT_CONFIGURED"

    serialized = json.dumps(body, ensure_ascii=False)
    assert "object_key" not in serialized
    assert "private-bucket" not in serialized
    assert "f" * 64 not in serialized
    # locator 只保留白名单字段（父块定位，来自 parent）
    assert set(body["locator"]) <= {"block_index", "page_start", "page_end", "bbox"}


# ---------------------------------------------------------------------------
# 8. 访问审计
# ---------------------------------------------------------------------------
def test_success_and_denial_are_audited(
    client: TestClient, admin: psycopg.Connection[Any], tenant: str, app_conn: psycopg.Connection[Any]
) -> None:
    seeded = _seed(admin, tenant)
    hidden = _seed(admin, tenant, child_acls=[{"allow": {"roles": ["manager"]}}])
    token = _token(tenant, seeded["principal_id"])

    ok = client.get(f"/v1/evidence/{seeded['child_ids'][0]}", headers=_auth(token))
    assert ok.status_code == 200
    denied = client.get(f"/v1/evidence/{hidden['child_ids'][0]}", headers=_auth(token))
    assert denied.status_code == 403

    set_tenant(app_conn, tenant)
    rows = app_conn.execute(
        """SELECT evidence_id, text_evidence_id, result, reason
             FROM evidence_access_log
            WHERE tenant_id = %s
            ORDER BY occurred_at, id""",
        (tenant,),
    ).fetchall()
    app_conn.rollback()

    results = {(str(row[0]), row[2], row[3]) for row in rows}
    assert (seeded["child_ids"][0], "ALLOWED", "AUTHORIZED") in results
    assert (hidden["child_ids"][0], "DENIED", "ACL_DENIED") in results
    allowed_row = next(row for row in rows if row[2] == "ALLOWED")
    assert str(allowed_row[1]) == seeded["parent_id"]  # 实际正文来源是父块


def test_audit_is_tenant_isolated(
    client: TestClient, admin: psycopg.Connection[Any], tenant: str, app_conn: psycopg.Connection[Any]
) -> None:
    seeded = _seed(admin, tenant)
    other = create_tenant(admin, f"审计对照 {uuid.uuid4().hex[:6]}")
    admin.commit()
    token = _token(tenant, seeded["principal_id"])
    assert client.get(f"/v1/evidence/{seeded['child_ids'][0]}", headers=_auth(token)).status_code == 200

    set_tenant(app_conn, other)
    visible = app_conn.execute("SELECT count(*) FROM evidence_access_log").fetchone()
    app_conn.rollback()
    assert visible is not None and int(visible[0]) == 0


def test_audit_failure_blocks_text(
    pg_url: str, admin: psycopg.Connection[Any], tenant: str
) -> None:
    """审计写不进去时不得返回正文：宁可 500，也不能留下"看过但没记录"的访问。"""
    seeded = _seed(admin, tenant)
    token = _token(tenant, seeded["principal_id"])
    admin.execute(
        """CREATE OR REPLACE FUNCTION fkh_test_block_evidence_audit() RETURNS trigger
           LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'audit blocked (test)'; END; $$"""
    )
    admin.execute(
        """CREATE TRIGGER trg_test_block_evidence_audit
           BEFORE INSERT ON evidence_access_log
           FOR EACH ROW EXECUTE FUNCTION fkh_test_block_evidence_audit()"""
    )
    admin.commit()
    # 审计失败会以异常形式冒到 ASGI 层；这里要看的是"响应里没有正文"，
    # 所以关掉 TestClient 的异常透传，按真实部署（500 响应）观察
    app = create_app(Settings(jwt_secret=SECRET, database_url=pg_url))
    try:
        with TestClient(app, raise_server_exceptions=False) as raw_client:
            response = raw_client.get(
                f"/v1/evidence/{seeded['child_ids'][0]}", headers=_auth(token)
            )
        assert response.status_code == 500
        assert "父块全文" not in response.text
    finally:
        admin.execute("DROP TRIGGER IF EXISTS trg_test_block_evidence_audit ON evidence_access_log")
        admin.execute("DROP FUNCTION IF EXISTS fkh_test_block_evidence_audit()")
        admin.commit()


def test_audit_is_append_only(
    client: TestClient, admin: psycopg.Connection[Any], tenant: str
) -> None:
    seeded = _seed(admin, tenant)
    token = _token(tenant, seeded["principal_id"])
    assert client.get(f"/v1/evidence/{seeded['child_ids'][0]}", headers=_auth(token)).status_code == 200

    with pytest.raises(psycopg.errors.CheckViolation):
        admin.execute("UPDATE evidence_access_log SET reason = 'AUTHORIZED'")
    admin.rollback()

    with pytest.raises(psycopg.errors.CheckViolation):
        admin.execute("DELETE FROM evidence_access_log")
    admin.rollback()


def test_invalid_uuid_is_validation_error(
    client: TestClient, admin: psycopg.Connection[Any], tenant: str
) -> None:
    seeded = _seed(admin, tenant)
    token = _token(tenant, seeded["principal_id"])
    response = client.get("/v1/evidence/not-a-uuid", headers=_auth(token))
    assert response.status_code == 422
