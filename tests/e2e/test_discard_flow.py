"""版本作废（DISCARDED）端到端。

逐条对应 2026-11-05 L1 裁决 B 的「需要的断言」
（`_evidence/l1_reply_discarded_status_20261105.md`）：

- 允许 RAW / DRAFT / REVIEWED / APPROVED_INTERNAL → DISCARDED；
- 拒绝 PUBLISHED_PUBLIC / SUPERSEDED / REVOKED → DISCARDED；
- 弃用原子写入状态 + 审计（含操作者、原因、来源状态）+ outbox（恰好一条）；
- 重复弃用幂等，不产生新审计/事件；
- DISCARDED 终态、不可复活；版本号不回收；
- 审批人保留（从 APPROVED_INTERNAL 弃用时）；
- 检索不可见；培训课程转 NEEDS_REVIEW；跨租户行为与撤销一致。
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any
from uuid import UUID

import psycopg
import pytest
from jsonschema import Draft202012Validator
from tests.conftest import create_principal

from fkh.authz.policy import AclPolicy, Subject
from fkh.domain.review import ReviewConflict, ReviewService, VersionNotFound
from fkh.domain.state_machine import IllegalTransition, VersionStatus
from fkh.retrieval.lexical import LexicalRetriever
from fkh.workers.ingestion import IngestionRequest, ingest_document

pytestmark = pytest.mark.requires_pg

ROOT = Path(__file__).resolve().parents[2]
ENVELOPE_SCHEMA = json.loads(
    (ROOT / "contracts" / "schema" / "event_envelope.json").read_text(encoding="utf-8")
)
CONTENT = "# 冷凝管说明\n\n高硼硅玻璃冷凝管，接口 24/29。\n"


def _ingest(
    conn: psycopg.Connection[Any],
    seed: dict[str, str],
    *,
    key: str | None = None,
    classification: int = 1,
    audience: str = "internal",
    content: str | None = None,
) -> UUID:
    request = IngestionRequest(
        source_id=f"discard-{uuid.uuid4().hex[:8]}",
        filename="discard.md",
        content=(content or CONTENT).encode(),
        knowledge_key=key or f"discard-{uuid.uuid4().hex[:8]}",
        title="冷凝管说明",
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


def _discard(conn: psycopg.Connection[Any], tenant: str, version: UUID, actor: str) -> Any:
    service = ReviewService(conn, UUID(tenant))
    snapshot = service.discard(version, UUID(actor), "传错文件")
    conn.commit()
    return snapshot


def _audit_rows(admin: psycopg.Connection[Any], version: UUID) -> list[tuple[Any, ...]]:
    rows = admin.execute(
        """SELECT action, from_status, reason, actor_id
             FROM review_actions WHERE version_id = %s ORDER BY created_at, id""",
        (version,),
    ).fetchall()
    return rows


def _outbox_rows(admin: psycopg.Connection[Any], version: UUID) -> list[tuple[Any, ...]]:
    return admin.execute(
        """SELECT event_type, payload, event_ordinal
             FROM outbox_events WHERE aggregate_id = %s ORDER BY event_ordinal""",
        (version,),
    ).fetchall()


# ---------------------------------------------------------------------------
# 1. 允许的来源状态
# ---------------------------------------------------------------------------
def test_discard_from_draft_writes_status_audit_and_single_event(
    app_conn: psycopg.Connection[Any], admin: psycopg.Connection[Any], seed: dict[str, str]
) -> None:
    version = _ingest(app_conn, seed)
    snapshot = _discard(app_conn, seed["tenant_a"], version, seed["user_a"])
    assert snapshot.status is VersionStatus.DISCARDED

    audits = [row for row in _audit_rows(admin, version) if row[0] == "DISCARD"]
    assert len(audits) == 1
    assert audits[0][1] == "DRAFT"  # 来源状态
    assert audits[0][2] == "传错文件"  # 原因
    assert str(audits[0][3]) == seed["user_a"]  # 操作者

    events = [row for row in _outbox_rows(admin, version) if row[0] == "knowledge.version.discarded.v1"]
    assert len(events) == 1
    envelope = {
        "schema_version": "1.0",
        "event_id": str(uuid.uuid4()),
        "event_type": events[0][0],
        "tenant_id": seed["tenant_a"],
        "aggregate_id": str(version),
        "aggregate_version": 1,
        "occurred_at": "2026-11-05T00:00:00Z",
        "trace_id": "t-discard",
        "producer": "knowledge-api",
        "payload": events[0][1],
    }
    Draft202012Validator(ENVELOPE_SCHEMA).validate(envelope)
    assert events[0][1]["from_status"] == "DRAFT"


def test_discard_from_reviewed_and_approved_internal(
    app_conn: psycopg.Connection[Any], admin: psycopg.Connection[Any], seed: dict[str, str]
) -> None:
    tenant = UUID(seed["tenant_a"])
    actor = UUID(seed["user_a"])

    reviewed = _ingest(app_conn, seed)
    ReviewService(app_conn, tenant).pass_review(reviewed, actor)
    app_conn.commit()
    assert _discard(app_conn, seed["tenant_a"], reviewed, seed["user_a"]).status is VersionStatus.DISCARDED

    approved = _ingest(app_conn, seed)
    service = ReviewService(app_conn, tenant)
    service.pass_review(approved, actor)
    service.approve_internal(approved, actor)
    app_conn.commit()
    snapshot = _discard(app_conn, seed["tenant_a"], approved, seed["user_a"])
    assert snapshot.status is VersionStatus.DISCARDED
    # 裁决 Q4：审批是已发生的历史事实，弃用不清理
    assert snapshot.approved_by == actor


# ---------------------------------------------------------------------------
# 2. 拒绝的来源状态
# ---------------------------------------------------------------------------
def _publish(app_conn: psycopg.Connection[Any], admin: psycopg.Connection[Any], seed: dict[str, str]) -> UUID:
    tenant = UUID(seed["tenant_a"])
    actor = UUID(seed["user_a"])
    publisher = UUID(str(create_principal(admin, seed["tenant_a"], f"pub-{uuid.uuid4().hex[:6]}@example.test")))
    admin.commit()
    version = _ingest(app_conn, seed, classification=0, audience="customer_public")
    service = ReviewService(app_conn, tenant)
    service.pass_review(version, actor)
    service.approve_internal(version, actor)
    service.publish_public(version, publisher)
    app_conn.commit()
    return version


def test_published_version_cannot_be_discarded(
    app_conn: psycopg.Connection[Any], admin: psycopg.Connection[Any], seed: dict[str, str]
) -> None:
    version = _publish(app_conn, admin, seed)
    with pytest.raises(IllegalTransition):
        ReviewService(app_conn, UUID(seed["tenant_a"])).discard(version, UUID(seed["user_a"]))
    app_conn.rollback()
    assert ReviewService(app_conn, UUID(seed["tenant_a"])).snapshot(version).status is VersionStatus.PUBLISHED_PUBLIC


def test_revoked_and_superseded_cannot_be_discarded(
    app_conn: psycopg.Connection[Any], admin: psycopg.Connection[Any], seed: dict[str, str]
) -> None:
    tenant = UUID(seed["tenant_a"])
    actor = UUID(seed["user_a"])
    service = ReviewService(app_conn, tenant)

    revoked = _publish(app_conn, admin, seed)
    service.revoke(revoked, actor)
    app_conn.commit()
    with pytest.raises(IllegalTransition):
        service.discard(revoked, actor)
    app_conn.rollback()

    superseded = _publish(app_conn, admin, seed)
    service._conn.execute(  # noqa: SLF001 - 直接构造 SUPERSEDED 需要绕过服务层动作
        "UPDATE knowledge_versions SET status = 'SUPERSEDED' WHERE tenant_id = %s AND id = %s",
        (tenant, superseded),
    )
    app_conn.commit()
    with pytest.raises(IllegalTransition):
        service.discard(superseded, actor)
    app_conn.rollback()


# ---------------------------------------------------------------------------
# 3. 幂等与终态
# ---------------------------------------------------------------------------
def test_repeated_discard_is_idempotent(
    app_conn: psycopg.Connection[Any], admin: psycopg.Connection[Any], seed: dict[str, str]
) -> None:
    version = _ingest(app_conn, seed)
    first = _discard(app_conn, seed["tenant_a"], version, seed["user_a"])
    second = _discard(app_conn, seed["tenant_a"], version, seed["user_a"])
    assert first.status is second.status is VersionStatus.DISCARDED

    assert len([r for r in _audit_rows(admin, version) if r[0] == "DISCARD"]) == 1
    assert len([r for r in _outbox_rows(admin, version) if r[0] == "knowledge.version.discarded.v1"]) == 1


def test_discarded_is_terminal(
    app_conn: psycopg.Connection[Any], seed: dict[str, str]
) -> None:
    tenant = UUID(seed["tenant_a"])
    actor = UUID(seed["user_a"])
    version = _ingest(app_conn, seed)
    _discard(app_conn, seed["tenant_a"], version, seed["user_a"])
    service = ReviewService(app_conn, tenant)

    for action in (
        lambda: service.pass_review(version, actor),
        lambda: service.approve_internal(version, actor),
        lambda: service.publish_public(version, actor),
        lambda: service.revoke(version, actor),
    ):
        with pytest.raises((IllegalTransition, ReviewConflict)):
            action()
        app_conn.rollback()
    # 重复弃用不报错（幂等），状态不变
    assert service.discard(version, actor).status is VersionStatus.DISCARDED


# ---------------------------------------------------------------------------
# 4. 检索不可见、版本号不回收
# ---------------------------------------------------------------------------
def test_discarded_version_is_invisible_to_retrieval(
    app_conn: psycopg.Connection[Any], seed: dict[str, str]
) -> None:
    tenant = UUID(seed["tenant_a"])
    version = _ingest(app_conn, seed)
    subject = Subject(
        principal_id=seed["user_a"], tenant_id=seed["tenant_a"], roles=frozenset({"staff"})
    )
    # 弃用前：草稿本来就不可见；这里对比的是 APPROVED_INTERNAL 弃用前后
    service = ReviewService(app_conn, tenant)
    service.pass_review(version, UUID(seed["user_a"]))
    service.approve_internal(version, UUID(seed["user_a"]))
    app_conn.commit()
    before = LexicalRetriever(app_conn, tenant).search(
        "冷凝管", knowledge_scope="internal_product", subject=subject, max_results=10
    )
    assert before.hits, "弃用前应当可见"

    _discard(app_conn, seed["tenant_a"], version, seed["user_a"])
    after = LexicalRetriever(app_conn, tenant).search(
        "冷凝管", knowledge_scope="internal_product", subject=subject, max_results=10
    )
    assert after.hits == ()


def test_version_number_is_not_reused_after_discard(
    app_conn: psycopg.Connection[Any], admin: psycopg.Connection[Any], seed: dict[str, str]
) -> None:
    key = f"reuse-{uuid.uuid4().hex[:6]}"
    # 同文档不同版本必须是不同内容（内容哈希唯一约束），否则会被判重复入库
    first = _ingest(app_conn, seed, key=key, content=CONTENT + "\n\n补充：第一批。\n")
    _discard(app_conn, seed["tenant_a"], first, seed["user_a"])
    second = _ingest(app_conn, seed, key=key, content=CONTENT + "\n\n补充：第二批。\n")

    rows = admin.execute(
        "SELECT version_no FROM knowledge_versions WHERE id IN (%s, %s) ORDER BY version_no",
        (first, second),
    ).fetchall()
    assert [row[0] for row in rows] == [1, 2], "版本号不回收，新版本继续递增"


# ---------------------------------------------------------------------------
# 5. 培训联动：弃用同样触发 NEEDS_REVIEW，且**已分配学员的访问入口被阻断**
# ---------------------------------------------------------------------------
def test_discard_marks_course_and_blocks_enrolled_learner(
    app_conn: psycopg.Connection[Any], admin: psycopg.Connection[Any], seed: dict[str, str]
) -> None:
    """L1 终审第 1 条：不能只证明"禁止新分配"，要证明已获得课程的学员也学不了。"""
    from fkh.domain.training import TrainingError, TrainingService

    tenant = UUID(seed["tenant_a"])
    actor = UUID(seed["user_a"])
    service = ReviewService(app_conn, tenant)

    version = _ingest(app_conn, seed)
    service.pass_review(version, actor)
    service.approve_internal(version, actor)
    app_conn.commit()

    training = TrainingService(app_conn, tenant)
    role_id = training.ensure_role(f"role-{uuid.uuid4().hex[:6]}", "装配工")
    course_id = training.create_course(role_id=role_id, title="冷凝管装配")
    training.add_source(course_id, version)
    course_version = training.snapshot(course_id).version
    question_id = training.create_question(
        course_id=course_id,
        course_version=course_version,
        question_type="SINGLE_CHOICE",
        prompt="冷凝管标准接口是？",
        answer_key={"correct": "A"},
        status="APPROVED",
    )
    training.publish_course(course_id)
    learner = "learner-enrolled@example.test"
    assignment_id = training.assign(course_id=course_id, learner_subject_id=learner)
    # 弃用前能正常考试——这是"已获得课程"的证据
    training.submit_attempt(
        assignment_id=assignment_id,
        learner_subject_id=learner,
        responses={str(question_id): "A"},
        idempotency_key="before-discard",
    )
    app_conn.commit()
    assert len(training.my_assignments(learner)) == 1

    _discard(app_conn, seed["tenant_a"], version, seed["user_a"])

    status = admin.execute(
        "SELECT status FROM training_courses WHERE tenant_id = %s AND course_id = %s",
        (tenant, course_id),
    ).fetchone()
    assert status[0] == "NEEDS_REVIEW", "引用被弃用版本的已发布课程必须转 NEEDS_REVIEW"

    # 三个学员入口都必须拒绝，而不只是"不能新分配"
    assert training.my_assignments(learner) == [], "学员任务列表不应再展示已作废课程"
    with pytest.raises(TrainingError):
        training.submit_attempt(
            assignment_id=assignment_id,
            learner_subject_id=learner,
            responses={str(question_id): "A"},
            idempotency_key="after-discard",
        )
    app_conn.rollback()
    with pytest.raises(TrainingError):
        training.progress(assignment_id, learner)
    app_conn.rollback()


# ---------------------------------------------------------------------------
# 6. 跨租户：与撤销一致（不可区分地拒绝）
# ---------------------------------------------------------------------------
def test_cross_tenant_discard_is_rejected_without_side_effects(
    app_conn: psycopg.Connection[Any], admin: psycopg.Connection[Any], seed: dict[str, str]
) -> None:
    version = _ingest(app_conn, seed)
    before_audit = len(_audit_rows(admin, version))
    before_outbox = len(_outbox_rows(admin, version))

    foreign = ReviewService(app_conn, UUID(seed["tenant_b"]))
    with pytest.raises(VersionNotFound):
        foreign.discard(version, UUID(seed["user_b"]))
    app_conn.rollback()

    assert len(_audit_rows(admin, version)) == before_audit
    assert len(_outbox_rows(admin, version)) == before_outbox
    assert ReviewService(app_conn, UUID(seed["tenant_a"])).snapshot(version).status is VersionStatus.DRAFT
