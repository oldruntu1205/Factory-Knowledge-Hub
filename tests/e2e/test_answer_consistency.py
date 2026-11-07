"""答案—证据一致性检查的端到端行为（2026-11-06 裁决 ①）。"""

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
from tests.conftest import create_principal, create_tenant

from fkh.api.main import create_app
from fkh.config import Settings
from fkh.generation import DeterministicGenerator

pytestmark = pytest.mark.requires_pg

SECRET = "consistency-secret-0123456789abcdef"
HASH_KEY = "consistency-hmac-key-0123456789"
HASH_KEY_ID = "k-consistency"
TEXT = "冷凝管标准口 24×24，标口从14-70mm，货号 A112420，单价 39.00 元。"


def _token(tenant: str, principal: str) -> str:
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
        "scope": ["knowledge.answer"],
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


@pytest.fixture
def seeded(
    admin: psycopg.Connection[Any], app_conn: psycopg.Connection[Any]
) -> Iterator[dict[str, Any]]:
    from fkh.authz.policy import AclPolicy
    from fkh.domain.review import ReviewService
    from fkh.workers.ingestion import IngestionRequest, ingest_document

    tenant = create_tenant(admin, f"一致 {uuid.uuid4().hex[:6]}")
    actor = create_principal(admin, tenant, f"c-{uuid.uuid4().hex[:6]}@example.test")
    admin.commit()
    result = ingest_document(
        app_conn,
        UUID(tenant),
        IngestionRequest(
            source_id=f"c-{uuid.uuid4().hex[:8]}",
            filename="c.md",
            content=TEXT.encode(),
            knowledge_key=f"c-{uuid.uuid4().hex[:6]}",
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
    review = ReviewService(app_conn, UUID(tenant))
    review.pass_review(result.version_id, UUID(actor))
    review.approve_internal(result.version_id, UUID(actor))
    app_conn.commit()

    app_conn.execute("SELECT set_config('app.tenant_id', %s, true)", (tenant,))
    # 证据块可能是"整段恢复的 parent"：引用 id 用它，才能对上 build_citations 的白名单
    chunk = (
        app_conn.execute(
            "SELECT id FROM knowledge_chunks WHERE tenant_id = %s AND chunk_kind = 'parent' LIMIT 1",
            (tenant,),
        ).fetchone()
        or app_conn.execute(
            "SELECT id FROM knowledge_chunks WHERE tenant_id = %s AND chunk_kind = 'child' LIMIT 1",
            (tenant,),
        ).fetchone()
    )[0]
    yield {
        "tenant": tenant,
        "actor": actor,
        "chunk": str(chunk),
        "token": _token(tenant, actor),
    }


def _use(monkeypatch: pytest.MonkeyPatch, text: str) -> None:
    generator = DeterministicGenerator(text)
    monkeypatch.setattr("fkh.api.main.get_generator", lambda *args, **kwargs: generator)


def _ask(client: TestClient, token: str) -> Any:
    return client.post(
        "/v1/knowledge/answer",
        json={
            "query": "冷凝管的规格说明是什么？",
            "knowledge_scope": "internal_product",
            "language": "zh-CN",
            "max_evidence": 5,
        },
        headers={"Authorization": f"Bearer {token}"},
    )


def test_supported_claims_pass_through(
    client: TestClient, seeded: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    _use(monkeypatch, f"货号 A112420 的单价为 39.00 元。\n引用：{seeded['chunk']}")
    response = _ask(client, seeded["token"])
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "ANSWERED"
    assert body["unsupported"] == {"codes": 0, "numbers": 0}


def test_unsupported_code_and_number_require_review(
    client: TestClient, seeded: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """答案里的货号/价格在引用证据中不存在 → 人工复核（不是直接拒答）。"""
    _use(monkeypatch, f"货号 A999999 的单价为 88.00 元。\n引用：{seeded['chunk']}")
    response = _ask(client, seeded["token"])
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "HUMAN_REVIEW_REQUIRED"
    assert body["refusal_code"] == "UNSUPPORTED_CLAIM"
    assert body["requires_human_review"] is True
    assert body["unsupported"] == {"codes": 1, "numbers": 1}
    assert body["citations"], "可用引用保留给复核人员"


def test_no_claims_reports_not_applicable(
    client: TestClient, seeded: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """没有货号/带单位数值的答案不做这项检查：不适用，而不是"100% 支持"。"""
    _use(monkeypatch, f"冷凝管采用高硼硅玻璃。\n引用：{seeded['chunk']}")
    response = _ask(client, seeded["token"])
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "ANSWERED"
    assert body.get("unsupported") is None


def test_audit_records_consistency_fields(
    client: TestClient,
    admin: psycopg.Connection[Any],
    seeded: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use(monkeypatch, f"货号 A999999 的单价为 88.00 元。\n引用：{seeded['chunk']}")
    response = _ask(client, seeded["token"])
    assert response.status_code == 200, response.text
    body = response.json()

    admin.execute("SELECT set_config('app.tenant_id', %s, false)", (seeded["tenant"],))
    row = admin.execute(
        """SELECT consistency_rule, codes_total, codes_supported, numbers_total,
                  numbers_supported, consistency_ms
             FROM rag_queries WHERE trace_id = %s""",
        (body["trace_id"],),
    ).fetchone()
    assert row is not None
    assert row[0] == "literal-support-v4"
    assert (row[1], row[2]) == (1, 0)
    assert (row[3], row[4]) == (1, 0)
    assert row[5] is not None and row[5] >= 0
