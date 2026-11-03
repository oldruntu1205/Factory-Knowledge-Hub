"""新人培训治理规则（P0）。

覆盖 L1 §13.7 的 TRAIN-EXAM-01/02、TRAIN-KNOW-02 与幂等要求。
"""

from __future__ import annotations

import uuid

import psycopg
import pytest
from tests.conftest import create_version

pytestmark = [pytest.mark.security, pytest.mark.requires_pg]


def _create_role(admin: psycopg.Connection, tenant: str, code: str = "foreign_trade_sales") -> str:
    role_id = str(uuid.uuid4())
    admin.execute(
        "INSERT INTO training_roles (tenant_id, role_id, code, name) VALUES (%s, %s, %s, %s)",
        (tenant, role_id, code, "外贸销售"),
    )
    return role_id


def _create_course(admin: psycopg.Connection, tenant: str, role_id: str, title: str = "冷凝管产品知识") -> str:
    course_id = str(uuid.uuid4())
    admin.execute(
        """INSERT INTO training_courses (tenant_id, course_id, role_id, title, status, version)
           VALUES (%s, %s, %s, %s, 'DRAFT', 1)""",
        (tenant, course_id, role_id, title),
    )
    return course_id


def _create_assignment(admin: psycopg.Connection, tenant: str, course_id: str, learner: str) -> str:
    assignment_id = str(uuid.uuid4())
    admin.execute(
        """INSERT INTO training_assignments (tenant_id, assignment_id, course_id, learner_subject_id, status)
           VALUES (%s, %s, %s, %s, 'IN_PROGRESS')""",
        (tenant, assignment_id, course_id, learner),
    )
    return assignment_id


def test_learner_cannot_sign_off_own_practical(admin: psycopg.Connection, seed: dict[str, str]) -> None:
    """TRAIN-EXAM-02：学员不得自行提交实操 PASS。"""
    role_id = _create_role(admin, seed["tenant_a"])
    course_id = _create_course(admin, seed["tenant_a"], role_id)
    assignment_id = _create_assignment(admin, seed["tenant_a"], course_id, "learner-001")

    with pytest.raises(psycopg.errors.CheckViolation):
        admin.execute(
            """INSERT INTO training_practical_signoffs
                 (tenant_id, signoff_id, assignment_id, assessor_subject_id, decision)
               VALUES (%s, %s, %s, 'learner-001', 'PASS')""",
            (seed["tenant_a"], str(uuid.uuid4()), assignment_id),
        )


def test_mentor_can_sign_off(admin: psycopg.Connection, seed: dict[str, str]) -> None:
    """正向对照：不同主体签核必须成功，否则等于把所有签核都拒了。"""
    role_id = _create_role(admin, seed["tenant_a"], code="product_tech")
    course_id = _create_course(admin, seed["tenant_a"], role_id, title="烧器工艺")
    assignment_id = _create_assignment(admin, seed["tenant_a"], course_id, "learner-002")

    admin.execute(
        """INSERT INTO training_practical_signoffs
             (tenant_id, signoff_id, assignment_id, assessor_subject_id, decision)
           VALUES (%s, %s, %s, 'mentor-007', 'PASS')""",
        (seed["tenant_a"], str(uuid.uuid4()), assignment_id),
    )
    row = admin.execute(
        "SELECT decision FROM training_practical_signoffs WHERE assignment_id = %s", (assignment_id,)
    ).fetchone()
    assert row is not None
    assert row[0] == "PASS"


def test_course_publish_requires_knowledge_source(admin: psycopg.Connection, seed: dict[str, str]) -> None:
    role_id = _create_role(admin, seed["tenant_a"], code="no_source_role")
    course_id = _create_course(admin, seed["tenant_a"], role_id, title="无知识源课程")

    with pytest.raises(psycopg.errors.CheckViolation):
        admin.execute("UPDATE training_courses SET status = 'PUBLISHED' WHERE course_id = %s", (course_id,))


def test_course_publish_rejects_unapproved_source(admin: psycopg.Connection, seed: dict[str, str]) -> None:
    role_id = _create_role(admin, seed["tenant_a"], code="draft_source_role")
    course_id = _create_course(admin, seed["tenant_a"], role_id, title="引用草稿的课程")
    draft_version = create_version(
        admin, seed["tenant_a"], seed["doc_a"], status="DRAFT", version_no=8801
    )
    admin.execute(
        """INSERT INTO training_course_sources (tenant_id, course_id, knowledge_version_id)
           VALUES (%s, %s, %s)""",
        (seed["tenant_a"], course_id, draft_version),
    )

    with pytest.raises(psycopg.errors.CheckViolation):
        admin.execute("UPDATE training_courses SET status = 'PUBLISHED' WHERE course_id = %s", (course_id,))


def test_revoking_knowledge_marks_course_needs_review(admin: psycopg.Connection, seed: dict[str, str]) -> None:
    """TRAIN-KNOW-02：SOP 撤销后课程必须转 NEEDS_REVIEW，且历史成绩不受影响。"""
    role_id = _create_role(admin, seed["tenant_a"], code="revoke_link_role")
    course_id = _create_course(admin, seed["tenant_a"], role_id, title="依赖 SOP 的课程")
    version_id = create_version(
        admin, seed["tenant_a"], seed["doc_a"], status="APPROVED_INTERNAL",
        approved_by=seed["user_a"], version_no=8802,
    )
    admin.execute(
        """INSERT INTO training_course_sources (tenant_id, course_id, knowledge_version_id)
           VALUES (%s, %s, %s)""",
        (seed["tenant_a"], course_id, version_id),
    )
    admin.execute("UPDATE training_courses SET status = 'PUBLISHED' WHERE course_id = %s", (course_id,))

    status = admin.execute(
        "SELECT status FROM training_courses WHERE course_id = %s", (course_id,)
    ).fetchone()
    assert status is not None and status[0] == "PUBLISHED"

    admin.execute("UPDATE knowledge_versions SET status = 'REVOKED' WHERE id = %s", (version_id,))

    after = admin.execute(
        "SELECT status FROM training_courses WHERE course_id = %s", (course_id,)
    ).fetchone()
    assert after is not None and after[0] == "NEEDS_REVIEW"


def test_duplicate_attempt_idempotency_key_rejected(admin: psycopg.Connection, seed: dict[str, str]) -> None:
    """TRAIN-EXAM-03：同一幂等键不得产生重复成绩。"""
    role_id = _create_role(admin, seed["tenant_a"], code="idem_role")
    course_id = _create_course(admin, seed["tenant_a"], role_id, title="幂等课程")
    assignment_id = _create_assignment(admin, seed["tenant_a"], course_id, "learner-003")

    admin.execute(
        """INSERT INTO training_attempts
             (tenant_id, attempt_id, assignment_id, course_version, responses,
              objective_score, grading_status, idempotency_key)
           VALUES (%s, %s, %s, 1, '{}'::jsonb, 80, 'AUTO_GRADED', 'idem-key-1')""",
        (seed["tenant_a"], str(uuid.uuid4()), assignment_id),
    )

    with pytest.raises(psycopg.errors.UniqueViolation):
        admin.execute(
            """INSERT INTO training_attempts
                 (tenant_id, attempt_id, assignment_id, course_version, responses,
                  objective_score, grading_status, idempotency_key)
               VALUES (%s, %s, %s, 1, '{}'::jsonb, 100, 'AUTO_GRADED', 'idem-key-1')""",
            (seed["tenant_a"], str(uuid.uuid4()), assignment_id),
        )


def test_duplicate_assignment_rejected(admin: psycopg.Connection, seed: dict[str, str]) -> None:
    role_id = _create_role(admin, seed["tenant_a"], code="dup_assign_role")
    course_id = _create_course(admin, seed["tenant_a"], role_id, title="重复分配课程")
    _create_assignment(admin, seed["tenant_a"], course_id, "learner-004")

    with pytest.raises(psycopg.errors.UniqueViolation):
        _create_assignment(admin, seed["tenant_a"], course_id, "learner-004")
