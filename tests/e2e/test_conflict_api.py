"""目录冲突 HTTP 接口：可见性、权限、决议、并发与契约（2026-11-07 L1 裁决）。"""

from __future__ import annotations

import hashlib
import json
import threading
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import jwt
import psycopg
import pytest
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator
from tests.conftest import create_principal, create_tenant, set_tenant

from fkh.api.main import create_app
from fkh.authz.policy import AclPolicy
from fkh.catalog.conflicts import add_candidates, create_group
from fkh.config import Settings
from fkh.domain.review import ReviewService
from fkh.workers.ingestion import IngestionRequest, ingest_document

pytestmark = pytest.mark.requires_pg

SECRET = "conflict-api-secret-0123456789"
HASH_KEY = "conflict-api-hmac-key-0123456789"
HASH_KEY_ID = "k-conf-api"
ROOT = Path(__file__).resolve().parents[2]
ERROR_SCHEMA = json.loads((ROOT / "contracts" / "schema" / "error.json").read_text("utf-8"))
LIST_SCHEMA = json.loads(
    (ROOT / "contracts" / "schema" / "catalog_conflict_list.json").read_text("utf-8")
)

LINE_A = "A112420 200mm 38*300 24*24 39.00"
LINE_B = "A112420 300mm 38*400 24*24 48.00"


def _token(
    tenant: str,
    principal: str,
    *,
    scopes: tuple[str, ...] = ("catalog.read", "catalog.resolve"),
    allowed: tuple[str, ...] = ("internal_product",),
) -> str:
    now = datetime.now(UTC)
    payload = {
        "iss": "fkh.local",
        "aud": "fkh-api",
        "sub": f"user-{principal[:8]}@example.test",
        "iat": now,
        "exp": now + timedelta(minutes=10),
        "tenant_id": tenant,
        "principal_id": principal,
        "roles": ["staff"],
        "scope": list(scopes),
        "allowed_scopes": list(allowed),
    }
    return jwt.encode(payload, SECRET, algorithm="HS256")


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


def _ingest(conn: psycopg.Connection[Any], tenant: str, actor: str) -> Any:
    result = ingest_document(
        conn,
        UUID(tenant),
        IngestionRequest(
            source_id=f"capi-{uuid.uuid4().hex[:8]}",
            filename="catalog.md",
            content=("# 目录\n\nA112420 200mm 39.00\n").encode(),
            knowledge_key=f"capi-{uuid.uuid4().hex[:6]}",
            title="目录",
            domain="product_catalog",
            owner_department="sales",
            classification=2,
            audience="internal",
            required_scope="internal_product",
            acl_policy=AclPolicy.from_json({"allow": {"roles": ["staff"]}, "deny": {}}),
            actor_id=UUID(actor),
            catalog_currency="CNY",
        ),
    )
    conn.commit()
    return result


def _seed_group(
    admin: psycopg.Connection[Any],
    app_conn: psycopg.Connection[Any],
    *,
    code: str = "A112420",
    approve: bool = True,
    version_status: str | None = None,
) -> dict[str, Any]:
    tenant = create_tenant(admin, f"冲突API {uuid.uuid4().hex[:6]}")
    actor = create_principal(admin, tenant, f"ops-{uuid.uuid4().hex[:6]}@example.test")
    admin.commit()

    result = _ingest(app_conn, tenant, actor)
    version = result.version_id
    set_tenant(admin, tenant)
    acl = admin.execute(
        "SELECT acl_policy_id FROM knowledge_chunks WHERE tenant_id = %s AND version_id = %s LIMIT 1",
        (tenant, version),
    ).fetchone()[0]

    version_id = UUID(str(version))
    app_conn.execute("SELECT set_config('app.tenant_id', %s, true)", (tenant,))
    app_conn.execute(
        "DELETE FROM catalog_items WHERE tenant_id = %s AND version_id = %s AND code = %s",
        (tenant, version, code),
    )
    group = create_group(app_conn, UUID(tenant), version_id, code)
    row_a = app_conn.execute(
        """INSERT INTO knowledge_chunks
             (tenant_id, version_id, chunk_kind, chunk_text, chunk_sha256, tokenizer_id,
              lexeme_tokenizer_id, heading_path, locator, acl_policy_id, index_generation, lexeme)
           VALUES (%s, %s, 'child', %s, %s, 'approx-cjk-v1', 'cjk-char-v1', ARRAY['第 1 章'],
                   %s, %s, 1, to_tsvector('simple', %s)) RETURNING id""",
        (
            tenant,
            version,
            LINE_A,
            hashlib.sha256(LINE_A.encode()).hexdigest(),
            json.dumps({"page_start": 12}),
            acl,
            LINE_A,
        ),
    ).fetchone()[0]
    row_b = app_conn.execute(
        """INSERT INTO knowledge_chunks
             (tenant_id, version_id, chunk_kind, chunk_text, chunk_sha256, tokenizer_id,
              lexeme_tokenizer_id, heading_path, locator, acl_policy_id, index_generation, lexeme)
           VALUES (%s, %s, 'child', %s, %s, 'approx-cjk-v1', 'cjk-char-v1', ARRAY['第 1 章'],
                   %s, %s, 1, to_tsvector('simple', %s)) RETURNING id""",
        (
            tenant,
            version,
            LINE_B,
            hashlib.sha256(LINE_B.encode()).hexdigest(),
            json.dumps({"page_start": 13}),
            acl,
            LINE_B,
        ),
    ).fetchone()[0]
    add_candidates(
        app_conn,
        UUID(tenant),
        group,
        [
            (row_a, LINE_A, "200mm 38*300 24*24", "39.00", 39.00, {"page_start": 12}),
            (row_b, LINE_B, "300mm 38*400 24*24", "48.00", 48.00, {"page_start": 13}),
        ],
    )
    if approve:
        review = ReviewService(app_conn, UUID(tenant))
        review.pass_review(version, UUID(actor))
        review.approve_internal(version, UUID(actor))
    if version_status is not None:
        # 用请求侧连接改状态，避免测试管理员连接与请求事务互相等待（实测会卡住）
        app_conn.execute("SELECT set_config('app.tenant_id', %s, true)", (tenant,))
        app_conn.execute(
            "UPDATE knowledge_versions SET status = %s WHERE id = %s", (version_status, version)
        )
        app_conn.commit()
    app_conn.commit()
    return {
        "tenant": tenant,
        "actor": actor,
        "version": str(version),
        "group": str(group),
        "token": _token(tenant, actor),
    }


def _candidate_ids(admin: psycopg.Connection[Any], tenant: str, group: str) -> list[str]:
    set_tenant(admin, tenant)
    rows = admin.execute(
        "SELECT candidate_id, raw_price FROM catalog_conflict_candidates "
        "WHERE tenant_id = %s AND group_id = %s ORDER BY raw_price DESC",
        (tenant, group),
    ).fetchall()
    return [str(row[0]) for row in rows]


def _get(client: TestClient, token: str, **params: Any) -> Any:
    return client.get(
        "/v1/catalog/conflicts",
        params=params,
        headers={"Authorization": f"Bearer {token}"},
    )


def _resolve(client: TestClient, token: str, group: str, body: dict[str, Any]) -> Any:
    return client.post(
        f"/v1/catalog/conflicts/{group}/resolve",
        json=body,
        headers={"Authorization": f"Bearer {token}"},
    )


def _assert_error(response: Any, code: str) -> None:
    body = response.json()
    Draft202012Validator(ERROR_SCHEMA).validate(body)
    assert body["error"]["code"] == code


def test_list_returns_open_groups_without_raw_text(
    client: TestClient,
    admin: psycopg.Connection[Any],
    app_conn: psycopg.Connection[Any],
) -> None:
    seeded = _seed_group(admin, app_conn)
    response = _get(client, seeded["token"])
    assert response.status_code == 200, response.text
    body = response.json()
    Draft202012Validator(LIST_SCHEMA).validate(body)
    assert body["total"] == 1
    item = body["items"][0]
    assert item["code"] == "A112420"
    assert item["status"] == "OPEN"
    assert item["selected_candidate_id"] is None
    assert len(item["candidates"]) == 2
    assert "raw_text" not in item["candidates"][0], "裁决 5：响应不含原文整行"
    assert item["candidates"][0]["spec"] and item["candidates"][0]["locator"]


def test_list_filters_by_code_and_status_and_paginates(
    client: TestClient,
    admin: psycopg.Connection[Any],
    app_conn: psycopg.Connection[Any],
) -> None:
    seeded = _seed_group(admin, app_conn)
    assert _get(client, seeded["token"], code="ZZZZZZ").json()["total"] == 0
    page = _get(client, seeded["token"], offset=5).json()
    assert page["items"] == [] and page["total"] == 1, "越界返回空列表但与 total 一致"
    bad_limit = _get(client, seeded["token"], limit=201)
    assert bad_limit.status_code == 422
    bad_status = _get(client, seeded["token"], status="WHATEVER")
    assert bad_status.status_code == 422
    assert _get(client, seeded["token"], status="KEEP").json()["total"] == 0


def test_invisible_groups_are_filtered_before_paging(
    client: TestClient,
    admin: psycopg.Connection[Any],
    app_conn: psycopg.Connection[Any],
) -> None:
    """不可见（未审批）版本里的组：不进 items，也不进 total；分页不出现空洞。"""
    visible = _seed_group(admin, app_conn, code="A112420")
    hidden = _seed_group(admin, app_conn, code="A112421", version_status="SUPERSEDED")

    body = _get(client, visible["token"], limit=1).json()
    assert body["total"] == 1
    assert body["items"][0]["code"] == "A112420"

    second = _get(client, visible["token"], limit=1, offset=1).json()
    assert second["items"] == [], "隐藏组不能占用分页位置"

    hidden_tenant_token = _token(hidden["tenant"], hidden["actor"])
    assert _get(client, hidden_tenant_token).json()["total"] == 0


def test_scope_required_for_read_and_resolve(
    client: TestClient,
    admin: psycopg.Connection[Any],
    app_conn: psycopg.Connection[Any],
) -> None:
    seeded = _seed_group(admin, app_conn)
    read_only = _token(seeded["tenant"], seeded["actor"], scopes=("catalog.read",))
    assert _get(client, read_only).status_code == 200

    no_scope = _token(seeded["tenant"], seeded["actor"], scopes=("knowledge.answer",))
    _assert_error(_get(client, no_scope), "ACCESS_DENIED")

    candidate = _candidate_ids(admin, seeded["tenant"], seeded["group"])[0]
    _assert_error(
        _resolve(client, read_only, seeded["group"], {"action": "KEEP", "candidate_id": candidate}),
        "ACCESS_DENIED",
    )


def test_service_principal_cannot_resolve(
    client: TestClient,
    admin: psycopg.Connection[Any],
    app_conn: psycopg.Connection[Any],
) -> None:
    seeded = _seed_group(admin, app_conn)
    set_tenant(admin, seeded["tenant"])
    service = admin.execute(
        """INSERT INTO principals (tenant_id, external_subject, principal_type)
           VALUES (%s, %s, 'service') RETURNING id""",
        (seeded["tenant"], f"svc-{uuid.uuid4().hex[:6]}"),
    ).fetchone()[0]
    admin.commit()

    token = _token(seeded["tenant"], str(service))
    candidate = _candidate_ids(admin, seeded["tenant"], seeded["group"])[0]
    _assert_error(
        _resolve(client, token, seeded["group"], {"action": "KEEP", "candidate_id": candidate}),
        "ACCESS_DENIED",
    )
    assert _get(client, token).status_code == 200, "读取不额外禁止 service 主体"


def test_resolve_keep_and_drop_all(
    client: TestClient,
    admin: psycopg.Connection[Any],
    app_conn: psycopg.Connection[Any],
) -> None:
    seeded = _seed_group(admin, app_conn)
    candidates = _candidate_ids(admin, seeded["tenant"], seeded["group"])

    first = _resolve(
        client, seeded["token"], seeded["group"], {"action": "KEEP", "candidate_id": candidates[0],
                                                    "note": "以主表为准"}
    )
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["status"] == "KEEP"
    assert body["selected_candidate_id"] == candidates[0]
    assert body["note"] == "以主表为准"
    assert body["resolved_by"] == seeded["actor"]

    set_tenant(admin, seeded["tenant"])
    item = admin.execute(
        "SELECT count(*) FROM catalog_items WHERE tenant_id = %s AND code = 'A112420'",
        (seeded["tenant"],),
    ).fetchone()[0]
    assert item == 1, "KEEP 要把所选候选回填成条目"


def test_drop_all_and_duplicate_resolution_conflicts(
    client: TestClient,
    admin: psycopg.Connection[Any],
    app_conn: psycopg.Connection[Any],
) -> None:
    seeded = _seed_group(admin, app_conn, code="B223344")
    first = _resolve(client, seeded["token"], seeded["group"], {"action": "DROP_ALL"})
    assert first.status_code == 200, first.text
    assert first.json()["status"] == "DROP_ALL"
    assert first.json()["selected_candidate_id"] is None

    set_tenant(admin, seeded["tenant"])
    before = admin.execute(
        "SELECT resolved_at, resolved_by, note FROM catalog_conflict_groups WHERE group_id = %s",
        (seeded["group"],),
    ).fetchone()
    again = _resolve(client, seeded["token"], seeded["group"], {"action": "DROP_ALL"})
    _assert_error(again, "VERSION_CONFLICT")
    after = admin.execute(
        "SELECT resolved_at, resolved_by, note FROM catalog_conflict_groups WHERE group_id = %s",
        (seeded["group"],),
    ).fetchone()
    assert before == after, "重复决议不得改写首次决议字段"


def test_validation_rules_on_resolve(
    client: TestClient,
    admin: psycopg.Connection[Any],
    app_conn: psycopg.Connection[Any],
) -> None:
    seeded = _seed_group(admin, app_conn)
    group = seeded["group"]
    token = seeded["token"]
    candidates = _candidate_ids(admin, seeded["tenant"], group)

    _assert_error(_resolve(client, token, group, {"action": "KEEP"}), "VALIDATION_ERROR")
    _assert_error(
        _resolve(client, token, group, {"action": "DROP_ALL", "candidate_id": None}),
        "VALIDATION_ERROR",
    )

    other = _seed_group(admin, app_conn, code="C334455")
    foreign = _candidate_ids(admin, other["tenant"], other["group"])[0]
    _assert_error(
        _resolve(client, token, group, {"action": "KEEP", "candidate_id": foreign}),
        "VALIDATION_ERROR",
    )
    _assert_error(
        _resolve(client, token, group, {"action": "KEEP", "candidate_id": candidates[0], "actor": "x"}),
        "VALIDATION_ERROR",
    )


def test_cross_tenant_and_invisible_version_are_not_distinguishable(
    client: TestClient,
    admin: psycopg.Connection[Any],
    app_conn: psycopg.Connection[Any],
) -> None:
    seeded = _seed_group(admin, app_conn)
    other = _seed_group(admin, app_conn, code="D445566", version_status="REVOKED")

    candidate = _candidate_ids(admin, seeded["tenant"], seeded["group"])[0]
    _assert_error(
        _resolve(client, other["token"], seeded["group"], {"action": "KEEP", "candidate_id": candidate}),
        "NOT_FOUND",
    )
    _assert_error(_resolve(client, seeded["token"], str(uuid.uuid4()), {"action": "DROP_ALL"}), "NOT_FOUND")


def test_superseded_version_conflicts_before_resolution(
    client: TestClient,
    admin: psycopg.Connection[Any],
    app_conn: psycopg.Connection[Any],
) -> None:
    seeded = _seed_group(admin, app_conn)
    set_tenant(admin, seeded["tenant"])
    admin.execute(
        "UPDATE knowledge_versions SET status = 'SUPERSEDED' WHERE id = %s", (seeded["version"],)
    )
    admin.commit()

    candidate = _candidate_ids(admin, seeded["tenant"], seeded["group"])[0]
    response = _resolve(
        client, seeded["token"], seeded["group"], {"action": "KEEP", "candidate_id": candidate}
    )
    _assert_error(response, "VERSION_CONFLICT")


def test_concurrent_resolution_allows_exactly_one_winner(
    client: TestClient,
    admin: psycopg.Connection[Any],
    app_conn: psycopg.Connection[Any],
) -> None:
    seeded = _seed_group(admin, app_conn, code="E556677")
    candidate = _candidate_ids(admin, seeded["tenant"], seeded["group"])[0]
    results: list[int] = []
    lock = threading.Lock()

    def worker() -> None:
        response = _resolve(
            client, seeded["token"], seeded["group"], {"action": "KEEP", "candidate_id": candidate}
        )
        with lock:
            results.append(response.status_code)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(results) == [200, 409], results


def test_acl_denied_group_is_hidden_and_404_on_resolve(
    client: TestClient,
    admin: psycopg.Connection[Any],
    app_conn: psycopg.Connection[Any],
) -> None:
    """组内任一候选 chunk 的 ACL 拒绝 → 整组不可见；决议按资源级拒绝返回 404。"""
    seeded = _seed_group(admin, app_conn, code="G667788")
    assert _get(client, seeded["token"]).json()["total"] == 1

    # 把候选所在 chunk 的策略换成只允许 admin 角色
    app_conn.execute("SELECT set_config('app.tenant_id', %s, true)", (seeded["tenant"],))
    policy = app_conn.execute(
        """INSERT INTO acl_policies (tenant_id, policy_json) VALUES (%s, %s) RETURNING id""",
        (seeded["tenant"], json.dumps({"allow": {"roles": ["admin"]}, "deny": {}})),
    ).fetchone()[0]
    app_conn.execute(
        """UPDATE knowledge_chunks SET acl_policy_id = %s
            WHERE tenant_id = %s AND id IN (
              SELECT chunk_id FROM catalog_conflict_candidates WHERE group_id = %s)""",
        (policy, seeded["tenant"], seeded["group"]),
    )
    app_conn.commit()

    assert _get(client, seeded["token"]).json()["total"] == 0
    candidate = _candidate_ids(admin, seeded["tenant"], seeded["group"])[0]
    _assert_error(
        _resolve(client, seeded["token"], seeded["group"], {"action": "KEEP", "candidate_id": candidate}),
        "NOT_FOUND",
    )
