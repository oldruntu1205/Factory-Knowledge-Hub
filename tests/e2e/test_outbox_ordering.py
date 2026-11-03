"""发布后撤销：同一版本的两条 outbox 事件必须都能落库，并按业务顺序投递。

这条用例覆盖的是一个**只靠读代码看不出来**的缺陷：
outbox 的顺序唯一键是 (聚合, 类型, id, 版本, ordinal)，而发布与撤销同属一个版本。
两条事件若都用默认 ordinal=0，撤销会被唯一约束直接拒绝——即「撤销已发布内容」这条
L1 §7.2 的硬要求在生产上会失败，而现有用例只测了「批准后直接撤销」，碰不到它。
"""

from __future__ import annotations

import uuid
from typing import Any
from uuid import UUID

import psycopg
import pytest
from tests.conftest import create_principal

from fkh.authz.policy import AclPolicy
from fkh.domain.review import ReviewService
from fkh.workers.ingestion import IngestionRequest, ingest_document
from fkh.workers.outbox_relay import OutboxRelay, RelayConfig

pytestmark = pytest.mark.requires_pg

CONTENT = """# 冷凝管公开说明

高硼硅玻璃冷凝管适用于回流与蒸馏，标准接口为 24/29 与 29/32。
"""


class _NoopTransport:
    def deliver(self, envelope: dict[str, Any]) -> None:
        return None


def _ingest(conn: psycopg.Connection[Any], seed: dict[str, str]) -> UUID:
    request = IngestionRequest(
        source_id=f"src-{uuid.uuid4().hex[:8]}",
        filename="public-note.md",
        content=CONTENT.encode(),
        knowledge_key=f"ordering-{uuid.uuid4().hex[:8]}",
        title="冷凝管公开说明",
        domain="product_catalog",
        owner_department="tech",
        classification=0,
        audience="customer_public",
        required_scope="customer_public",
        acl_policy=AclPolicy.from_json({"allow": {"roles": ["staff"]}}),
        actor_id=UUID(seed["user_a"]),
    )
    result = ingest_document(conn, UUID(seed["tenant_a"]), request)
    conn.commit()
    return result.version_id


def test_publish_then_revoke_emits_ordered_events(
    admin: psycopg.Connection[Any],
    app_conn: psycopg.Connection[Any],
    relay_conn: psycopg.Connection[Any],
    seed: dict[str, str],
) -> None:
    version_id = _ingest(app_conn, seed)
    reviewer = UUID(seed["user_a"])
    publisher = UUID(str(create_principal(admin, seed["tenant_a"], "public-approver-order@example.test")))
    admin.commit()

    service = ReviewService(app_conn, UUID(seed["tenant_a"]))
    service.pass_review(version_id, reviewer)
    service.approve_internal(version_id, reviewer)
    service.publish_public(version_id, publisher)
    service.revoke(version_id, publisher, reason="内容有误")
    app_conn.commit()

    rows = admin.execute(
        """SELECT event_type, event_ordinal
             FROM outbox_events
            WHERE aggregate_id = %s
            ORDER BY event_ordinal""",
        (version_id,),
    ).fetchall()
    admin.rollback()

    assert [(row[0], row[1]) for row in rows] == [
        ("knowledge.version.published.v1", 1),
        ("knowledge.version.revoked.v1", 2),
    ]

    # 顺序键的用途：撤销不得先于发布被投递
    relay = OutboxRelay(relay_conn, _NoopTransport(), RelayConfig())
    first_batch = [e for e in relay.claim(20) if e.aggregate_id == version_id]
    assert [e.event_type for e in first_batch] == ["knowledge.version.published.v1"]

    relay.deliver_claimed(first_batch[0])
    second_batch = [e for e in relay.claim(20) if e.aggregate_id == version_id]
    assert [e.event_type for e in second_batch] == ["knowledge.version.revoked.v1"]
