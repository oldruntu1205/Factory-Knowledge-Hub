"""检索 API 端到端：词法召回 + ACL 前置 + 检索审计。

逐条对应 2026-11-03 L1 判决「必须补的测试」1–8 条（_evidence/l1_reply_search_20261103.md）。
这一层验证的是**从令牌到片段的完整链路**，以及几条容易被"看起来能用"掩盖的约束：
候选挤占（条数可以不足）、parent 不得作为普通命中、score 不外露、query 明文不落任何地方。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
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
)

from fkh.api.main import create_app
from fkh.config import Settings
from fkh.domain.lexeme import segment_cjk
from fkh.retrieval.search import query_fingerprint

pytestmark = pytest.mark.requires_pg

SECRET = "search-api-secret-0123456789abcdef"
HASH_KEY = "search-audit-hmac-key-0123456789"
HASH_KEY_ID = "k-test"
ROOT = Path(__file__).resolve().parents[2]
SEARCH_SCHEMA = json.loads(
    (ROOT / "contracts" / "schema" / "search_response.json").read_text(encoding="utf-8")
)
TEXT = "冷凝管规格说明：高硼硅玻璃，接口 24/29 与 29/32。"


def _token(
    tenant_id: str,
    principal_id: str,
    *,
    scopes: tuple[str, ...] = ("knowledge.search",),
    allowed: tuple[str, ...] = ("internal_product",),
    roles: tuple[str, ...] = ("staff",),
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
        "scope": list(scopes),
        "allowed_scopes": list(allowed),
    }
    return jwt.encode(payload, SECRET, algorithm="HS256")


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


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
def tenant(admin: psycopg.Connection[Any]) -> str:
    tenant_id = create_tenant(admin, f"检索租户 {uuid.uuid4().hex[:6]}")
    admin.commit()
    return tenant_id


def _chunk(
    admin: psycopg.Connection[Any],
    tenant_id: str,
    version_id: str,
    acl_id: str,
    *,
    text: str = TEXT,
    kind: str = "child",
    parent_id: str | None = None,
    tokenizer: str = "cjk-char-v1",
    locator: dict[str, Any] | None = None,
) -> str:
    row = admin.execute(
        """INSERT INTO knowledge_chunks
             (tenant_id, version_id, parent_chunk_id, chunk_kind, chunk_text, chunk_sha256,
              tokenizer_id, heading_path, locator, acl_policy_id, index_generation, lexeme)
           VALUES (%s, %s, %s, %s, %s, %s, %s, ARRAY['第 1 章'], %s::jsonb, %s, 1,
                   to_tsvector('simple', %s))
           RETURNING id""",
        (
            tenant_id,
            version_id,
            parent_id,
            kind,
            text,
            hashlib.sha256(text.encode()).hexdigest(),
            tokenizer,
            psycopg.types.json.Jsonb(locator or {"block_index": 0, "page_start": 1}),
            acl_id,
            segment_cjk(text),
        ),
    ).fetchone()
    assert row is not None
    return str(row[0])


def _version(
    admin: psycopg.Connection[Any],
    tenant_id: str,
    *,
    audience: str = "internal",
    status: str = "APPROVED_INTERNAL",
    required_scope: str | None = "internal_product",
    classification: int = 0,
) -> str:
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
    return version


def _search(client: TestClient, token: str, query: str = "冷凝", **overrides: Any) -> Any:
    payload = {"query": query, "knowledge_scope": "internal_product", "max_results": 10}
    payload.update(overrides)
    return client.post("/v1/knowledge/search", json=payload, headers=_auth(token))


def _ids(response: Any) -> set[str]:
    return {item["chunk_id"] for item in response.json()["results"]}


# ---------------------------------------------------------------------------
# 1. 不可见块不得返回任何字段
# ---------------------------------------------------------------------------
def test_invisible_chunks_never_appear(
    client: TestClient, admin: psycopg.Connection[Any], tenant: str
) -> None:
    version = _version(admin, tenant)
    visible = _chunk(admin, tenant, version, create_acl(admin, tenant, {"allow": {"roles": ["staff"]}}))
    denied = _chunk(
        admin, tenant, version, create_acl(admin, tenant, {"allow": {"roles": ["staff"]}, "deny": {"roles": ["staff"]}})
    )
    no_match = _chunk(admin, tenant, version, create_acl(admin, tenant, {"allow": {"roles": ["manager"]}}))
    empty_policy = _chunk(
        admin,
        tenant,
        version,
        # 注意：conftest 的 create_acl 把「空 dict」当作「没传」并用默认 allow=staff，
        # 这里要的是「显式空策略」（无人可读），所以必须写成带键的空对象
        create_acl(admin, tenant, {"allow": {}, "deny": {}}),
    )

    other = create_tenant(admin, f"别家 {uuid.uuid4().hex[:6]}")
    other_version = _version(admin, other)
    foreign = _chunk(admin, other, other_version, create_acl(admin, other, {"allow": {"roles": ["staff"]}}))
    admin.commit()

    token = _token(tenant, create_principal(admin, tenant, f"staff-{uuid.uuid4().hex[:6]}@example.test"))
    admin.commit()

    response = _search(client, token)
    assert response.status_code == 200
    found = _ids(response)
    assert visible in found
    for invisible in (denied, no_match, empty_policy, foreign):
        assert invisible not in found
    assert denied not in json.dumps(response.json())


# ---------------------------------------------------------------------------
# 2. 范围授权与精确匹配
# ---------------------------------------------------------------------------
def test_missing_scope_permission_is_403(
    client: TestClient, admin: psycopg.Connection[Any], tenant: str
) -> None:
    version = _version(admin, tenant)
    _chunk(admin, tenant, version, create_acl(admin, tenant, {"allow": {"roles": ["staff"]}}))
    principal = create_principal(admin, tenant, f"staff-{uuid.uuid4().hex[:6]}@example.test")
    admin.commit()

    # 有接口权限，但只被授权 customer_public
    token = _token(tenant, principal, allowed=("customer_public",))
    response = _search(client, token, knowledge_scope="internal_product")
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "ACCESS_DENIED"


def test_scope_mismatch_and_unassigned_scope_are_invisible(
    client: TestClient, admin: psycopg.Connection[Any], tenant: str
) -> None:
    training = _version(admin, tenant, required_scope="internal_training")
    training_chunk = _chunk(admin, tenant, training, create_acl(admin, tenant, {"allow": {"roles": ["staff"]}}))
    unassigned = _version(admin, tenant, required_scope=None)
    unassigned_chunk = _chunk(
        admin, tenant, unassigned, create_acl(admin, tenant, {"allow": {"roles": ["staff"]}})
    )
    mismatch = _version(admin, tenant, status="APPROVED_INTERNAL", audience="customer_public", required_scope="customer_public")
    mismatch_chunk = _chunk(
        admin, tenant, mismatch, create_acl(admin, tenant, {"allow": {"roles": ["staff"]}})
    )
    principal = create_principal(admin, tenant, f"staff-{uuid.uuid4().hex[:6]}@example.test")
    admin.commit()

    token = _token(tenant, principal, allowed=("internal_product", "customer_public"))
    found = _ids(_search(client, token))
    for hidden in (training_chunk, unassigned_chunk, mismatch_chunk):
        assert hidden not in found


# ---------------------------------------------------------------------------
# 3. parent 不得作为普通命中
# ---------------------------------------------------------------------------
def test_parent_is_never_returned_even_when_it_matches(
    client: TestClient, admin: psycopg.Connection[Any], tenant: str
) -> None:
    version = _version(admin, tenant)
    parent = _chunk(
        admin,
        tenant,
        version,
        create_acl(admin, tenant, {"allow": {"roles": ["staff"]}}),
        kind="parent",
        text=TEXT + "（父块聚合了子块内容）",
    )
    allowed_child = _chunk(
        admin,
        tenant,
        version,
        create_acl(admin, tenant, {"allow": {"roles": ["staff"]}}),
        parent_id=parent,
    )
    _chunk(
        admin,
        tenant,
        version,
        create_acl(admin, tenant, {"allow": {"roles": ["manager"]}}),
        parent_id=parent,
    )
    principal = create_principal(admin, tenant, f"staff-{uuid.uuid4().hex[:6]}@example.test")
    admin.commit()

    found = _ids(_search(client, _token(tenant, principal)))
    assert parent not in found
    assert allowed_child in found


# ---------------------------------------------------------------------------
# 4. 候选挤占：允许不足额，但不得超上限；空结果结构统一
# ---------------------------------------------------------------------------
def test_denied_candidates_may_underfill_but_never_exceed_limit(
    client: TestClient, admin: psycopg.Connection[Any], tenant: str
) -> None:
    version = _version(admin, tenant)
    allow = create_acl(admin, tenant, {"allow": {"roles": ["staff"]}})
    deny = create_acl(admin, tenant, {"allow": {"roles": ["manager"]}})
    for _ in range(10):
        _chunk(admin, tenant, version, deny, text=TEXT * 3)  # 更长 → rank 更高
    allowed = [_chunk(admin, tenant, version, allow) for _ in range(3)]
    principal = create_principal(admin, tenant, f"staff-{uuid.uuid4().hex[:6]}@example.test")
    admin.commit()

    response = _search(client, _token(tenant, principal), max_results=3)
    assert response.status_code == 200
    body = response.json()
    assert len(body["results"]) <= 3
    # 结构里不得出现候选数、过滤统计或分数
    assert set(body) == {"results", "scoring", "trace_id"}
    assert all(set(item) == {
        "chunk_id", "version_id", "knowledge_id", "text_snippet",
        "snippet_truncated", "locator", "heading_path",
    } for item in body["results"])
    assert len(body["results"]) >= 1
    assert set(_ids(response)) <= set(allowed)


def test_empty_and_fully_denied_share_same_structure(
    client: TestClient, admin: psycopg.Connection[Any], tenant: str
) -> None:
    version = _version(admin, tenant)
    _chunk(admin, tenant, version, create_acl(admin, tenant, {"allow": {"roles": ["manager"]}}))
    principal = create_principal(admin, tenant, f"staff-{uuid.uuid4().hex[:6]}@example.test")
    admin.commit()
    token = _token(tenant, principal)

    fully_denied = _search(client, token)
    no_match = _search(client, token, query="完全不相干的词条")
    assert fully_denied.status_code == no_match.status_code == 200
    for response in (fully_denied, no_match):
        body = response.json()
        assert body["results"] == []
        assert set(body) == {"results", "scoring", "trace_id"}


def test_equal_scores_have_stable_order(
    client: TestClient, admin: psycopg.Connection[Any], tenant: str
) -> None:
    version = _version(admin, tenant)
    acl = create_acl(admin, tenant, {"allow": {"roles": ["staff"]}})
    for _ in range(3):
        _chunk(admin, tenant, version, acl)
    principal = create_principal(admin, tenant, f"staff-{uuid.uuid4().hex[:6]}@example.test")
    admin.commit()
    token = _token(tenant, principal)

    orders = [response.json()["results"] for response in (_search(client, token), _search(client, token))]
    assert [item["chunk_id"] for item in orders[0]] == [item["chunk_id"] for item in orders[1]]


# ---------------------------------------------------------------------------
# 5. 分词与查询注入面
# ---------------------------------------------------------------------------
def test_query_with_tsquery_operators_is_safe(
    client: TestClient, admin: psycopg.Connection[Any], tenant: str
) -> None:
    version = _version(admin, tenant)
    chunk = _chunk(admin, tenant, version, create_acl(admin, tenant, {"allow": {"roles": ["staff"]}}))
    principal = create_principal(admin, tenant, f"staff-{uuid.uuid4().hex[:6]}@example.test")
    admin.commit()
    token = _token(tenant, principal)

    response = _search(client, token, query="冷凝 & | ! ( ) :*")
    assert response.status_code == 200  # 不因手拼 tsquery 而 500
    assert chunk in _ids(response)


def test_punctuation_only_query_returns_empty(
    client: TestClient, admin: psycopg.Connection[Any], tenant: str
) -> None:
    principal = create_principal(admin, tenant, f"staff-{uuid.uuid4().hex[:6]}@example.test")
    admin.commit()
    response = _search(client, _token(tenant, principal), query="！！！？？")
    assert response.status_code == 200
    assert response.json()["results"] == []


def test_chunks_with_other_tokenizer_are_not_matched(
    client: TestClient, admin: psycopg.Connection[Any], tenant: str
) -> None:
    version = _version(admin, tenant)
    foreign_tokenizer = _chunk(
        admin, tenant, version, create_acl(admin, tenant, {"allow": {"roles": ["staff"]}}), tokenizer="zhparser-v2"
    )
    principal = create_principal(admin, tenant, f"staff-{uuid.uuid4().hex[:6]}@example.test")
    admin.commit()

    assert foreign_tokenizer not in _ids(_search(client, _token(tenant, principal)))


# ---------------------------------------------------------------------------
# 6. 片段截断与不泄露
# ---------------------------------------------------------------------------
def test_snippet_is_capped_and_no_score_leaks(
    client: TestClient, admin: psycopg.Connection[Any], tenant: str
) -> None:
    version = _version(admin, tenant)
    long_text = "冷凝" + "玻璃仪器" * 200 + "𝄞" * 10
    _chunk(
        admin,
        tenant,
        version,
        create_acl(admin, tenant, {"allow": {"roles": ["staff"]}}),
        text=long_text,
    )
    principal = create_principal(admin, tenant, f"staff-{uuid.uuid4().hex[:6]}@example.test")
    admin.commit()

    response = _search(client, _token(tenant, principal))
    body = response.json()
    Draft202012Validator(SEARCH_SCHEMA, format_checker=FormatChecker()).validate(body)
    item = body["results"][0]
    assert len(item["text_snippet"]) <= 400
    assert item["snippet_truncated"] is True
    serialized = json.dumps(body, ensure_ascii=False)
    assert "score" not in serialized
    assert "index_generation" not in serialized
    assert "玻璃仪器" * 200 not in serialized  # 没有全文


# ---------------------------------------------------------------------------
# 7. 检索审计
# ---------------------------------------------------------------------------
def _audit_rows(admin: psycopg.Connection[Any], tenant: str) -> list[tuple[Any, ...]]:
    rows = admin.execute(
        """SELECT query_hash, query_hash_key_id, knowledge_scope, status, refusal_code,
                  returned_count, trace_id
             FROM retrieval_queries WHERE tenant_id = %s ORDER BY created_at, id""",
        (tenant,),
    ).fetchall()
    admin.rollback()
    return rows


def test_success_empty_and_refusal_are_audited(
    client: TestClient, admin: psycopg.Connection[Any], tenant: str
) -> None:
    version = _version(admin, tenant)
    _chunk(admin, tenant, version, create_acl(admin, tenant, {"allow": {"roles": ["staff"]}}))
    principal = create_principal(admin, tenant, f"staff-{uuid.uuid4().hex[:6]}@example.test")
    admin.commit()
    token = _token(tenant, principal)

    assert _search(client, token).status_code == 200
    assert _search(client, token, query="完全不相干的词条").json()["results"] == []
    assert _search(client, _token(tenant, principal, allowed=("customer_public",))).status_code == 403

    rows = _audit_rows(admin, tenant)
    statuses = [(row[3], row[4]) for row in rows]
    assert ("SUCCEEDED", None) in statuses
    assert ("REFUSED", "SCOPE_NOT_AUTHORIZED") in statuses
    counts = [row[5] for row in rows if row[3] == "SUCCEEDED"]
    assert 0 in counts and any(count and count > 0 for count in counts)


def test_execution_error_is_audited(
    client: TestClient, admin: psycopg.Connection[Any], tenant: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    principal = create_principal(admin, tenant, f"staff-{uuid.uuid4().hex[:6]}@example.test")
    admin.commit()
    token = _token(tenant, principal)

    import fkh.retrieval.search as search_module

    class _Boom:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def search(self, *args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("db exploded")

    monkeypatch.setattr(search_module, "LexicalRetriever", _Boom)
    response = _search(client, token)
    assert response.status_code == 500

    rows = [row for row in _audit_rows(admin, tenant) if row[3] == "ERROR"]
    assert rows and rows[0][4] == "EXECUTION_FAILED"
    assert rows[0][5] is None  # 无法确定返回数时留 NULL


def test_audit_failure_blocks_results(
    client: TestClient, admin: psycopg.Connection[Any], tenant: str
) -> None:
    version = _version(admin, tenant)
    _chunk(admin, tenant, version, create_acl(admin, tenant, {"allow": {"roles": ["staff"]}}))
    principal = create_principal(admin, tenant, f"staff-{uuid.uuid4().hex[:6]}@example.test")
    admin.commit()
    admin.execute(
        """CREATE OR REPLACE FUNCTION fkh_test_block_retrieval_audit() RETURNS trigger
           LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'audit blocked (test)'; END; $$"""
    )
    admin.execute(
        """CREATE TRIGGER trg_test_block_retrieval_audit
           BEFORE INSERT ON retrieval_queries
           FOR EACH ROW EXECUTE FUNCTION fkh_test_block_retrieval_audit()"""
    )
    admin.commit()
    try:
        response = _search(client, _token(tenant, principal))
        assert response.status_code == 500
        assert "results" not in response.text
        assert "冷凝管规格" not in response.text
    finally:
        admin.execute("DROP TRIGGER IF EXISTS trg_test_block_retrieval_audit ON retrieval_queries")
        admin.execute("DROP FUNCTION IF EXISTS fkh_test_block_retrieval_audit()")
        admin.commit()


def test_audit_is_append_only(
    client: TestClient, admin: psycopg.Connection[Any], tenant: str
) -> None:
    principal = create_principal(admin, tenant, f"staff-{uuid.uuid4().hex[:6]}@example.test")
    admin.commit()
    assert _search(client, _token(tenant, principal)).status_code == 200

    with pytest.raises(psycopg.errors.CheckViolation):
        admin.execute("UPDATE retrieval_queries SET status = 'REFUSED'")
    admin.rollback()

    with pytest.raises(psycopg.errors.CheckViolation):
        admin.execute("DELETE FROM retrieval_queries")
    admin.rollback()


def test_audit_is_tenant_isolated(
    client: TestClient, admin: psycopg.Connection[Any], tenant: str, app_conn: psycopg.Connection[Any]
) -> None:
    principal = create_principal(admin, tenant, f"staff-{uuid.uuid4().hex[:6]}@example.test")
    other = create_tenant(admin, f"审计对照 {uuid.uuid4().hex[:6]}")
    admin.commit()
    assert _search(client, _token(tenant, principal)).status_code == 200

    set_tenant(app_conn, other)
    visible = app_conn.execute("SELECT count(*) FROM retrieval_queries").fetchone()
    app_conn.rollback()
    assert visible is not None and int(visible[0]) == 0


# ---------------------------------------------------------------------------
# 8. query 明文不得出现在任何对外或日志出口
# ---------------------------------------------------------------------------
def test_query_plaintext_never_leaks(
    client: TestClient,
    admin: psycopg.Connection[Any],
    tenant: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    marker = "敏感查询-9f3c1e-冷凝"
    version = _version(admin, tenant)
    _chunk(admin, tenant, version, create_acl(admin, tenant, {"allow": {"roles": ["staff"]}}))
    principal = create_principal(admin, tenant, f"staff-{uuid.uuid4().hex[:6]}@example.test")
    admin.commit()
    token = _token(tenant, principal)

    caplog.set_level(logging.DEBUG)
    ok = _search(client, token, query=marker)
    invalid = _search(client, token, query=marker * 300)  # 超过 2000 长度 → 422

    assert ok.status_code == 200
    assert invalid.status_code == 422
    # 422 不得回显请求体（FastAPI 默认会带 input，这里必须被收敛掉）
    assert marker not in invalid.text
    assert marker not in caplog.text

    rows = _audit_rows(admin, tenant)
    assert all(marker not in str(row) for row in rows)
    expected = hmac.new(HASH_KEY.encode(), marker.encode(), hashlib.sha256).hexdigest()
    assert expected == query_fingerprint(HASH_KEY, marker)
    assert any(row[0] == expected and row[1] == HASH_KEY_ID for row in rows)
