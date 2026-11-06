"""目录结构化通道端到端：抽取 → 精确查询 → 问答路由 → 审计。

对应 2026-11-06 L1 裁决（`_evidence/l1_reply_catalog_exact_20261106.md`）的断言：
唯一键/冲突、版本状态与 ACL、跨租户、原文一致性、路由不误触发、审计恰好一条。
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import jwt
import psycopg
import pytest
from fastapi.testclient import TestClient
from tests.conftest import (
    create_principal,
    create_tenant,
)

from fkh.api.main import create_app
from fkh.authz.policy import AclPolicy
from fkh.config import Settings
from fkh.domain.review import ReviewService
from fkh.generation import DeterministicGenerator
from fkh.workers.ingestion import IngestionRequest, ingest_document

pytestmark = pytest.mark.requires_pg

SECRET = "catalog-api-secret-0123456789abcdef"
HASH_KEY = "catalog-audit-hmac-key-0123456789"
HASH_KEY_ID = "k-cat"
CATALOG_TEXT = """# 冷凝管目录

货号 规格 管径*总长 上标口*下标口 单价
A112420 200mm 38*300 24*24 39.00
A112430 300mm 38*400 24*24 48.00
A999999 区间价示例 12.00-15.00
说明文字：以上价格含税。
"""


def _token(tenant_id: str, principal_id: str, *, scopes: tuple[str, ...] = ("knowledge.answer",)) -> str:
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
        "scope": list(scopes),
        "allowed_scopes": ["internal_product"],
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


def _ingest(
    conn: psycopg.Connection[Any],
    tenant_id: str,
    actor_id: str,
    *,
    domain: str = "product_catalog",
    currency: str | None = "CNY",
    text: str = CATALOG_TEXT,
    key: str | None = None,
) -> UUID:
    request = IngestionRequest(
        source_id=f"cat-{uuid.uuid4().hex[:8]}",
        filename="catalog.md",
        content=text.encode(),
        knowledge_key=key or f"catalog-{uuid.uuid4().hex[:8]}",
        title="冷凝管目录",
        domain=domain,
        owner_department="sales",
        classification=2,
        audience="internal",
        required_scope="internal_product",
        acl_policy=AclPolicy.from_json({"allow": {"roles": ["staff"]}, "deny": {}}),
        actor_id=UUID(actor_id),
        catalog_currency=currency,
    )
    return ingest_document(conn, UUID(tenant_id), request).version_id


def _approve(conn: psycopg.Connection[Any], tenant_id: str, actor_id: str, version_id: UUID) -> None:
    service = ReviewService(conn, UUID(tenant_id))
    service.pass_review(version_id, UUID(actor_id))
    service.approve_internal(version_id, UUID(actor_id))
    conn.commit()


def _setup(admin: psycopg.Connection[Any], app_conn: psycopg.Connection[Any]) -> dict[str, str]:
    tenant = create_tenant(admin, f"目录租户 {uuid.uuid4().hex[:6]}")
    actor = create_principal(admin, tenant, f"lib-{uuid.uuid4().hex[:6]}@example.test")
    admin.commit()
    version = _ingest(app_conn, tenant, actor)
    _approve(app_conn, tenant, actor, version)
    return {"tenant": tenant, "actor": actor, "version": str(version), "token": _token(tenant, actor)}


def _ask(client: TestClient, token: str, query: str) -> Any:
    return client.post(
        "/v1/knowledge/answer",
        json={
            "query": query,
            "knowledge_scope": "internal_product",
            "language": "zh-CN",
            "max_evidence": 5,
        },
        headers={"Authorization": f"Bearer {token}"},
    )


def _audit(admin: psycopg.Connection[Any], tenant_id: str) -> list[tuple[Any, ...]]:
    return admin.execute(
        """SELECT status, refusal_code, model_id, route, route_reason
             FROM rag_queries WHERE tenant_id = %s ORDER BY created_at""",
        (tenant_id,),
    ).fetchall()


# ---------------------------------------------------------------------------
# 抽取
# ---------------------------------------------------------------------------
def test_ingest_extracts_catalog_items_and_counts_unsupported(
    admin: psycopg.Connection[Any], app_conn: psycopg.Connection[Any]
) -> None:
    seeded = _setup(admin, app_conn)
    rows = admin.execute(
        "SELECT code, spec, raw_price, price, currency FROM catalog_items WHERE tenant_id = %s ORDER BY code",
        (seeded["tenant"],),
    ).fetchall()
    assert [row[0] for row in rows] == ["A112420", "A112430"]
    assert rows[0][2] == "39.00" and str(rows[0][3]) == "39.00"
    assert rows[0][4] == "CNY"
    # 区间价不入表
    assert all(row[0] != "A999999" for row in rows)


def test_non_catalog_domain_or_missing_currency_extracts_nothing(
    admin: psycopg.Connection[Any], app_conn: psycopg.Connection[Any]
) -> None:
    tenant = create_tenant(admin, f"非目录 {uuid.uuid4().hex[:6]}")
    actor = create_principal(admin, tenant, f"lib-{uuid.uuid4().hex[:6]}@example.test")
    admin.commit()

    other = _ingest(app_conn, tenant, actor, domain="glass_materials")
    no_currency = _ingest(app_conn, tenant, actor, currency=None)
    assert admin.execute(
        "SELECT count(*) FROM catalog_items WHERE version_id IN (%s, %s)", (other, no_currency)
    ).fetchone()[0] == 0


# ---------------------------------------------------------------------------
# 精确通道
# ---------------------------------------------------------------------------
def test_exact_hit_answers_without_model(
    admin: psycopg.Connection[Any], app_conn: psycopg.Connection[Any], client: TestClient
) -> None:
    seeded = _setup(admin, app_conn)
    body = _ask(client, seeded["token"], "货号 A112420 的单价是多少？").json()

    assert body["status"] == "ANSWERED"
    assert "39.00" in body["answer"]
    assert len(body["citations"]) == 1
    citation = body["citations"][0]
    assert citation["version_id"] == seeded["version"]
    chunk = admin.execute(
        "SELECT chunk_text FROM knowledge_chunks WHERE id = %s", (citation["evidence_id"],)
    ).fetchone()
    assert chunk and "A112420" in str(chunk[0]) and "39.00" in str(chunk[0])

    status, code, model_id, route, reason = _audit(admin, seeded["tenant"])[0]
    assert (status, code, model_id, route, reason) == ("ANSWERED", None, "catalog-exact:v1", "catalog_exact", "accepted")


def test_exact_miss_refused_without_model(
    admin: psycopg.Connection[Any], app_conn: psycopg.Connection[Any], client: TestClient
) -> None:
    seeded = _setup(admin, app_conn)
    body = _ask(client, seeded["token"], "货号 Z999999 的单价是多少？").json()
    assert body["status"] == "REFUSED"
    assert body["refusal_code"] == "NO_EVIDENCE"
    assert body["citations"] == []
    assert _audit(admin, seeded["tenant"])[0][3:5] == ("catalog_exact", "not_found")


def test_multi_code_and_out_of_intent_do_not_take_over(
    admin: psycopg.Connection[Any], app_conn: psycopg.Connection[Any], client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seeded = _setup(admin, app_conn)
    # 用确定性生成器兜住 RAG 路径，避免真调模型
    monkeypatch.setattr(
        "fkh.api.main.get_generator",
        lambda *a, **k: DeterministicGenerator("暂无法回答。"),
    )
    _ask(client, seeded["token"], "A112420 和 A112430 的单价分别是多少？")
    _ask(client, seeded["token"], "货号 A112420 停产了吗？")

    routes = [(row[3], row[4]) for row in _audit(admin, seeded["tenant"])]
    assert ("rag", "multi_code") in routes
    assert ("rag", "code_without_intent") in routes


def test_revoked_version_hides_items(
    admin: psycopg.Connection[Any], app_conn: psycopg.Connection[Any], client: TestClient
) -> None:
    seeded = _setup(admin, app_conn)
    ReviewService(app_conn, UUID(seeded["tenant"])).revoke(
        UUID(seeded["version"]), UUID(seeded["actor"])
    )
    app_conn.commit()

    body = _ask(client, seeded["token"], "A112420 多少钱？").json()
    assert body["status"] == "REFUSED"
    assert body["refusal_code"] == "NO_EVIDENCE"


def test_ambiguous_versions_refused_not_guessed(
    admin: psycopg.Connection[Any], app_conn: psycopg.Connection[Any], client: TestClient
) -> None:
    tenant = create_tenant(admin, f"冲突租户 {uuid.uuid4().hex[:6]}")
    actor = create_principal(admin, tenant, f"lib-{uuid.uuid4().hex[:6]}@example.test")
    admin.commit()

    first = _ingest(app_conn, tenant, actor, text="A112420 200mm 39.00\n")
    _approve(app_conn, tenant, actor, first)
    second = _ingest(
        app_conn, tenant, actor, text="A112420 200mm 41.00\n", key=f"other-{uuid.uuid4().hex[:6]}"
    )
    _approve(app_conn, tenant, actor, second)

    body = _ask(client, _token(tenant, actor), "A112420 的单价是多少？").json()
    assert body["status"] == "REFUSED"
    assert body["refusal_code"] == "CONFLICTING_EVIDENCE"
    assert body["citations"] == []
    assert _audit(admin, tenant)[-1][4] == "ambiguous"


def test_structured_data_inconsistent_with_source_requires_review(
    admin: psycopg.Connection[Any], app_conn: psycopg.Connection[Any], client: TestClient
) -> None:
    seeded = _setup(admin, app_conn)
    admin.execute(
        "UPDATE catalog_items SET raw_text = 'A112420 被篡改的行 99.00' WHERE tenant_id = %s AND code = 'A112420'",
        (seeded["tenant"],),
    )
    admin.commit()

    body = _ask(client, seeded["token"], "A112420 单价").json()
    assert body["status"] == "HUMAN_REVIEW_REQUIRED"
    assert body["citations"] == []
    assert _audit(admin, seeded["tenant"])[-1][4] == "inconsistent"


def test_cross_tenant_lookup_is_miss(
    admin: psycopg.Connection[Any], app_conn: psycopg.Connection[Any], client: TestClient
) -> None:
    _setup(admin, app_conn)  # 造一个持有该条目的租户，验证其他租户查不到
    other_tenant = create_tenant(admin, f"旁观租户 {uuid.uuid4().hex[:6]}")
    other_actor = create_principal(admin, other_tenant, f"x-{uuid.uuid4().hex[:6]}@example.test")
    admin.commit()

    body = _ask(client, _token(other_tenant, other_actor), "A112420 的单价是多少？").json()
    assert body["status"] == "REFUSED"
    assert body["refusal_code"] == "NO_EVIDENCE"


def test_backfill_is_idempotent(
    admin: psycopg.Connection[Any], app_conn: psycopg.Connection[Any]
) -> None:
    from fkh.workers.repository import IngestionRepository

    seeded = _setup(admin, app_conn)
    repo = IngestionRepository(app_conn, UUID(seeded["tenant"]))
    stats = repo.extract_catalog_items(version_id=UUID(seeded["version"]), currency="CNY")
    app_conn.commit()

    assert stats.inserted == 0
    assert stats.duplicates == 2
    assert stats.conflicts == 0


def test_same_version_conflict_is_not_served(
    admin: psycopg.Connection[Any], app_conn: psycopg.Connection[Any], client: TestClient
) -> None:
    """同版本内同货号出现不同原文行（来自不同块）：不得保留任一条。"""
    import hashlib

    from fkh.domain.lexeme import segment_cjk
    from fkh.workers.repository import IngestionRepository

    tenant = create_tenant(admin, f"同版冲突 {uuid.uuid4().hex[:6]}")
    actor = create_principal(admin, tenant, f"lib-{uuid.uuid4().hex[:6]}@example.test")
    admin.commit()

    version = _ingest(app_conn, tenant, actor, text="A112420 200mm 39.00\n")
    _approve(app_conn, tenant, actor, version)

    extra = "A112420 300mm 48.00"
    admin.execute(
        """INSERT INTO knowledge_chunks
             (tenant_id, version_id, chunk_kind, chunk_text, chunk_sha256,
              tokenizer_id, lexeme_tokenizer_id, heading_path, locator, acl_policy_id,
              index_generation, lexeme)
           SELECT %s, %s, 'child', %s, %s, 'approx-cjk-v1', 'cjk-char-v1',
                  ARRAY['第 1 章'], '{}'::jsonb, id, 1, to_tsvector('simple', %s)
             FROM acl_policies WHERE tenant_id = %s LIMIT 1""",
        (tenant, version, extra, hashlib.sha256(extra.encode()).hexdigest(), segment_cjk(extra), tenant),
    )
    admin.commit()

    stats = IngestionRepository(app_conn, UUID(tenant)).extract_catalog_items(
        version_id=UUID(str(version)), currency="CNY"
    )
    app_conn.commit()
    assert stats.conflicts >= 1

    rows = admin.execute(
        "SELECT count(*) FROM catalog_items WHERE tenant_id = %s AND code = 'A112420'", (tenant,)
    ).fetchone()[0]
    assert rows == 0, "冲突行不得保留任一条"

    body = _ask(client, _token(tenant, actor), "A112420 的单价是多少？").json()
    assert body["status"] == "REFUSED"
    assert body["refusal_code"] == "NO_EVIDENCE"
