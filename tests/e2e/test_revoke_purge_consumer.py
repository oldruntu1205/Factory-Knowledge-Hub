"""撤销清理的持久消费闭环验收（L1 2026-11-08 阻断项 3）。

真实 PostgreSQL：
- 撤销 → 事件在 outbox → 消费一次后块被删、台账 DONE；
- 再消费一次：幂等，不重复处理（attempts 不变）；
- 注入故障：失败累加 attempts 并转 FAILED（死信），`reset_failed` 后可重入并最终完成。
"""

from __future__ import annotations

import uuid
from typing import Any

import psycopg
import pytest
from tests.conftest import create_principal, create_tenant

from fkh.domain.review import ReviewService
from fkh.workers.index_maintenance import (
    mark_generation_ready,
    purge_version_chunks,
    switch_visible_generation,
)
from fkh.workers.ingestion import IngestionRequest, ingest_document
from fkh.workers.revoke_purge import (
    CONSUMER,
    MAX_ATTEMPTS,
    consume_revoke_events,
    failed_deliveries,
    reset_failed,
)

pytestmark = pytest.mark.requires_pg

DOC = "冷凝管检验规范\n\n气密性检验：充气至 0.2MPa，保压 60 秒，压力下降不超过 5% 为合格。\n"


@pytest.fixture
def world(admin: psycopg.Connection[Any]) -> dict[str, Any]:
    tenant = create_tenant(admin, f"清理 {uuid.uuid4().hex[:6]}")
    actor = create_principal(admin, tenant, f"actor-{uuid.uuid4().hex[:6]}@example.test")
    admin.commit()
    return {"tenant": tenant, "actor": actor}


def _ingest_and_publish(conn: psycopg.Connection[Any], world: dict[str, Any]) -> uuid.UUID:
    from fkh.authz.policy import AclPolicy

    result = ingest_document(
        conn,
        world["tenant"],
        IngestionRequest(
            source_id="doc-1",
            filename="spec.md",
            content=DOC.encode(),
            knowledge_key=f"k-{uuid.uuid4().hex[:8]}",
            title="检验规范",
            domain="quality",
            owner_department="qc",
            classification=1,
            audience="internal",
            required_scope="internal_product",
            acl_policy=AclPolicy.from_json({"allow": {"roles": ["staff"]}, "deny": {}}),
            actor_id=world["actor"],
        ),
    )
    mark_generation_ready(conn, world["tenant"], result.index_generation, 2)
    switch_visible_generation(conn, world["tenant"], result.index_generation)
    conn.commit()
    review = ReviewService(conn, world["tenant"])
    review.pass_review(result.version_id, world["actor"])
    review.approve_internal(result.version_id, world["actor"])
    conn.commit()
    return result.version_id


def _chunk_count(conn: psycopg.Connection[Any], tenant: uuid.UUID, version_id: uuid.UUID) -> int:
    return int(
        conn.execute(
            "SELECT count(*) FROM knowledge_chunks WHERE tenant_id = %s AND version_id = %s",
            (tenant, version_id),
        ).fetchone()[0]
    )


def test_consumer_purges_revoked_chunks_idempotently(
    admin: psycopg.Connection[Any], world: dict[str, Any]
) -> None:
    with psycopg.connect(admin.dsn if hasattr(admin, "dsn") else admin.info.dsn) as conn:
        conn.execute("SELECT set_config('app.tenant_id', %s, false)", (str(world["tenant"]),))
        version_id = _ingest_and_publish(conn, world)
        ReviewService(conn, world["tenant"]).revoke(version_id, world["actor"], "consumer-test")
        conn.commit()
        assert _chunk_count(conn, world["tenant"], version_id) > 0

        first = consume_revoke_events(conn, world["tenant"])
        assert first.processed == 1, first
        assert _chunk_count(conn, world["tenant"], version_id) == 0

        second = consume_revoke_events(conn, world["tenant"])
        assert second.processed == 0, "已 DONE 的事件必须幂等跳过"

        row = conn.execute(
            "SELECT status, attempts FROM outbox_deliveries"
            " WHERE tenant_id = %s AND consumer = %s",
            (world["tenant"], CONSUMER),
        ).fetchone()
        assert row == ("DONE", 1), row


def test_consumer_records_dead_letter_and_can_resume(
    admin: psycopg.Connection[Any], world: dict[str, Any]
) -> None:
    with psycopg.connect(admin.info.dsn) as conn:
        conn.execute("SELECT set_config('app.tenant_id', %s, false)", (str(world["tenant"]),))
        version_id = _ingest_and_publish(conn, world)
        ReviewService(conn, world["tenant"]).revoke(version_id, world["actor"], "dead-letter")
        conn.commit()

        def broken(_conn: psycopg.Connection[Any], _tenant: uuid.UUID, _version: uuid.UUID) -> int:
            raise RuntimeError("模拟删除失败")

        for _ in range(MAX_ATTEMPTS):
            result = consume_revoke_events(conn, world["tenant"], purge=broken)
            assert result.failed >= 1

        dead = failed_deliveries(conn, world["tenant"])
        assert dead, "重试耗尽必须留下死信记录"
        assert dead[0]["attempts"] >= MAX_ATTEMPTS
        assert "模拟删除失败" in (dead[0]["last_error"] or "")
        assert _chunk_count(conn, world["tenant"], version_id) > 0, "失败时不得误删"

        assert reset_failed(conn, world["tenant"]) == 1
        done = consume_revoke_events(conn, world["tenant"], purge=purge_version_chunks)
        assert done.processed == 1, done
        assert _chunk_count(conn, world["tenant"], version_id) == 0
