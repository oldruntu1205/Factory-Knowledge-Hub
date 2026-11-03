"""审核 → 内部批准 → 对外发布 → 撤销，端到端。

重点不是"状态能改"，而是三条硬约束：
1. 对外发布必须两个不同主体，同一人双签要被拒；
2. C1 及以上不得对外发布，即便双签齐全；
3. 撤销立即生效，不等索引清理。
"""

from __future__ import annotations

import uuid
from typing import Any
from uuid import UUID

import psycopg
import pytest
from tests.conftest import create_principal

from fkh.authz.policy import AclPolicy
from fkh.domain.review import ReviewConflict, ReviewService, VersionNotFound
from fkh.domain.state_machine import VersionStatus
from fkh.workers.ingestion import IngestionRequest, ingest_document

pytestmark = pytest.mark.requires_pg

CONTENT = """# 冷凝管公开说明

高硼硅玻璃冷凝管适用于回流与蒸馏，标准接口为 24/29 与 29/32。

## 可选规格

长度 300 mm、400 mm、500 mm。具体交期与价格需业务确认。
"""


def _ingest(
    conn: psycopg.Connection[Any],
    seed: dict[str, str],
    *,
    classification: int = 0,
    audience: str = "customer_public",
) -> UUID:
    request = IngestionRequest(
        source_id=f"src-{uuid.uuid4().hex[:8]}",
        filename="public-note.md",
        content=CONTENT.encode(),
        knowledge_key=f"public-{uuid.uuid4().hex[:8]}",
        title="冷凝管公开说明",
        domain="product_catalog",
        owner_department="tech",
        classification=classification,
        audience=audience,
        required_scope=("customer_public" if audience == "customer_public" else "internal_product"),
        acl_policy=AclPolicy.from_json({"allow": {"roles": ["staff"]}}),
        actor_id=UUID(seed["user_a"]),
    )
    result = ingest_document(conn, UUID(seed["tenant_a"]), request)
    conn.commit()
    return result.version_id


def test_full_publish_chain(
    app_conn: psycopg.Connection[Any], admin: psycopg.Connection[Any], seed: dict[str, str]
) -> None:
    version_id = _ingest(app_conn, seed)
    reviewer = UUID(seed["user_a"])
    publisher = UUID(str(create_principal(admin, seed["tenant_a"], "public-approver@example.test")))
    admin.commit()

    service = ReviewService(app_conn, UUID(seed["tenant_a"]))
    assert service.pass_review(version_id, reviewer).status is VersionStatus.REVIEWED
    assert service.approve_internal(version_id, reviewer).status is VersionStatus.APPROVED_INTERNAL
    published = service.publish_public(version_id, publisher)
    app_conn.commit()

    assert published.status is VersionStatus.PUBLISHED_PUBLIC
    assert published.approved_by == reviewer
    assert published.public_approved_by == publisher

    actions = admin.execute(
        "SELECT action FROM review_actions WHERE version_id = %s ORDER BY created_at",
        (version_id,),
    ).fetchall()
    assert [row[0] for row in actions] == ["SUBMIT", "REVIEW_PASS", "APPROVE_INTERNAL", "PUBLISH"]


def test_same_person_cannot_publish(
    admin: psycopg.Connection[Any], app_conn: psycopg.Connection[Any], seed: dict[str, str]
) -> None:
    version_id = _ingest(app_conn, seed)
    reviewer = UUID(seed["user_a"])

    service = ReviewService(app_conn, UUID(seed["tenant_a"]))
    service.pass_review(version_id, reviewer)
    service.approve_internal(version_id, reviewer)

    with pytest.raises(ReviewConflict, match="同一主体"):
        service.publish_public(version_id, reviewer)
    app_conn.rollback()


def test_c1_cannot_be_published_publicly(
    admin: psycopg.Connection[Any], app_conn: psycopg.Connection[Any], seed: dict[str, str]
) -> None:
    """C1 内容即使双签齐全也必须被拒，这是分级的意义所在。"""
    version_id = _ingest(app_conn, seed, classification=1, audience="customer_public")
    reviewer = UUID(seed["user_a"])
    publisher = UUID(str(create_principal(admin, seed["tenant_a"], "approver-c1@example.test")))
    admin.commit()

    service = ReviewService(app_conn, UUID(seed["tenant_a"]))
    service.pass_review(version_id, reviewer)
    service.approve_internal(version_id, reviewer)

    with pytest.raises(Exception, match="C1"):
        service.publish_public(version_id, publisher)
    app_conn.rollback()


def test_c0_without_internal_audience_cannot_publish(
    admin: psycopg.Connection[Any], app_conn: psycopg.Connection[Any], seed: dict[str, str]
) -> None:
    version_id = _ingest(app_conn, seed, classification=0, audience="internal")
    reviewer = UUID(seed["user_a"])
    publisher = UUID(str(create_principal(admin, seed["tenant_a"], "approver-aud@example.test")))
    admin.commit()

    service = ReviewService(app_conn, UUID(seed["tenant_a"]))
    service.pass_review(version_id, reviewer)
    service.approve_internal(version_id, reviewer)

    with pytest.raises(Exception, match="audience"):
        service.publish_public(version_id, publisher)
    app_conn.rollback()


def test_revoke_takes_effect_immediately(
    app_conn: psycopg.Connection[Any], seed: dict[str, str]
) -> None:
    version_id = _ingest(app_conn, seed)
    actor = UUID(seed["user_a"])
    service = ReviewService(app_conn, UUID(seed["tenant_a"]))
    service.pass_review(version_id, actor)
    service.approve_internal(version_id, actor)
    service.revoke(version_id, actor, reason="内容有误")
    app_conn.commit()

    assert service.snapshot(version_id).status is VersionStatus.REVOKED

    # 撤销后不可复活
    with pytest.raises(ReviewConflict):
        service.pass_review(version_id, actor)
    app_conn.rollback()


def test_revoked_version_cannot_be_reapproved(
    app_conn: psycopg.Connection[Any], seed: dict[str, str]
) -> None:
    version_id = _ingest(app_conn, seed)
    actor = UUID(seed["user_a"])
    service = ReviewService(app_conn, UUID(seed["tenant_a"]))
    service.pass_review(version_id, actor)
    service.approve_internal(version_id, actor)
    service.revoke(version_id, actor)
    app_conn.commit()

    with pytest.raises(ReviewConflict):
        service.approve_internal(version_id, actor)
    app_conn.rollback()


def test_draft_cannot_be_revoked_pending_adr(
    app_conn: psycopg.Connection[Any], seed: dict[str, str]
) -> None:
    """DRAFT 目前没有终止路径，这是 L1 §3.2 未定义的缺口。

    这里固定当前行为，避免有人"顺手"加一条 DRAFT→REVOKED 迁移表项而绕过架构裁决。
    方案与影响见 docs/REVIEW-REQUEST-draft-termination.md。
    """
    from fkh.domain.state_machine import IllegalTransition

    version_id = _ingest(app_conn, seed)
    service = ReviewService(app_conn, UUID(seed["tenant_a"]))

    with pytest.raises(IllegalTransition):
        service.revoke(version_id, UUID(seed["user_a"]))
    app_conn.rollback()


def test_review_requires_draft_state(
    app_conn: psycopg.Connection[Any], seed: dict[str, str]
) -> None:
    version_id = _ingest(app_conn, seed)
    actor = UUID(seed["user_a"])
    service = ReviewService(app_conn, UUID(seed["tenant_a"]))
    service.pass_review(version_id, actor)

    with pytest.raises(ReviewConflict, match="DRAFT"):
        service.pass_review(version_id, actor)
    app_conn.rollback()


def test_outbox_event_carries_no_sensitive_payload(
    app_conn: psycopg.Connection[Any], admin: psycopg.Connection[Any], seed: dict[str, str]
) -> None:
    version_id = _ingest(app_conn, seed)
    reviewer = UUID(seed["user_a"])
    publisher = UUID(str(create_principal(admin, seed["tenant_a"], "publisher-ob@example.test")))
    admin.commit()

    service = ReviewService(app_conn, UUID(seed["tenant_a"]))
    service.pass_review(version_id, reviewer)
    service.approve_internal(version_id, reviewer)
    service.publish_public(version_id, publisher)
    app_conn.commit()

    rows = admin.execute(
        "SELECT event_type, payload FROM outbox_events WHERE aggregate_id = %s", (version_id,)
    ).fetchall()
    event_types = {row[0] for row in rows}
    assert "knowledge.version.published.v1" in event_types

    for _event_type, payload in rows:
        assert "chunk_text" not in payload
        assert "query" not in payload
        assert len(str(payload)) < 500


def test_other_tenant_cannot_touch_version(
    app_conn: psycopg.Connection[Any], seed: dict[str, str]
) -> None:
    """跨租户访问一律表现为"版本不存在"，不泄露存在性。"""
    version_id = _ingest(app_conn, seed)

    foreign = ReviewService(app_conn, UUID(seed["tenant_b"]))
    with pytest.raises(VersionNotFound):
        foreign.snapshot(version_id)
    app_conn.rollback()
