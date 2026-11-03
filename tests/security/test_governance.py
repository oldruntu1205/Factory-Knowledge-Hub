"""知识状态机、版本不可变与审批隔离（P0）。

除负向用例外，这里包含一条完整正向链路。只有负向用例的测试集无法区分
"正确拒绝"和"把所有路径都拒绝"，因此正向链路是必需的反向证明。
"""

from __future__ import annotations

import itertools
import uuid

import psycopg
import pytest
from tests.conftest import create_principal, create_version

pytestmark = [pytest.mark.security, pytest.mark.requires_pg]

# 种子数据已占用 version_no=1；统一用独立区段避免唯一约束碰撞
_version_seq = itertools.count(1000)


def _fresh_version(admin: psycopg.Connection, seed: dict[str, str], **kwargs: object) -> str:
    kwargs.setdefault("version_no", next(_version_seq))
    return create_version(admin, seed["tenant_a"], seed["doc_a"], **kwargs)  # type: ignore[arg-type]


def test_illegal_transition_raw_to_published_rejected(admin: psycopg.Connection, seed: dict[str, str]) -> None:
    version = _fresh_version(admin, seed, status="RAW", classification=0, audience="customer_public")
    with pytest.raises(psycopg.errors.CheckViolation):
        admin.execute("UPDATE knowledge_versions SET status = 'PUBLISHED_PUBLIC' WHERE id = %s", (version,))


def test_skipping_review_step_rejected(admin: psycopg.Connection, seed: dict[str, str]) -> None:
    version = _fresh_version(admin, seed, status="DRAFT")
    with pytest.raises(psycopg.errors.CheckViolation):
        admin.execute("UPDATE knowledge_versions SET status = 'APPROVED_INTERNAL' WHERE id = %s", (version,))


def test_publish_requires_two_distinct_approvers(admin: psycopg.Connection, seed: dict[str, str]) -> None:
    version = _fresh_version(
        admin,
        seed,
        status="APPROVED_INTERNAL",
        classification=0,
        audience="customer_public",
        approved_by=seed["user_a"],
    )
    with pytest.raises(psycopg.errors.CheckViolation):
        admin.execute(
            """UPDATE knowledge_versions
                  SET status = 'PUBLISHED_PUBLIC', public_approved_by = %s
                WHERE id = %s""",
            (seed["user_a"], version),
        )


def test_publish_rejects_non_public_classification(admin: psycopg.Connection, seed: dict[str, str]) -> None:
    """classification=C0 才可公开；C1 即使双签也不得进入外部可见状态。"""
    other = create_principal(admin, seed["tenant_a"], f"public-approver-{uuid.uuid4().hex[:8]}@example.test")
    version = _fresh_version(
        admin,
        seed,
        status="APPROVED_INTERNAL",
        classification=1,
        audience="customer_public",
        approved_by=seed["user_a"],
    )
    with pytest.raises(psycopg.errors.CheckViolation):
        admin.execute(
            """UPDATE knowledge_versions
                  SET status = 'PUBLISHED_PUBLIC', public_approved_by = %s
                WHERE id = %s""",
            (other, version),
        )


def test_version_content_is_immutable(admin: psycopg.Connection, seed: dict[str, str]) -> None:
    version = _fresh_version(admin, seed, status="DRAFT")
    with pytest.raises(psycopg.errors.CheckViolation):
        admin.execute("UPDATE knowledge_versions SET title = '改了标题' WHERE id = %s", (version,))


def test_version_cannot_be_deleted(admin: psycopg.Connection, seed: dict[str, str]) -> None:
    version = _fresh_version(admin, seed, status="DRAFT")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        admin.execute("DELETE FROM knowledge_versions WHERE id = %s", (version,))


def test_review_actions_cannot_be_updated(admin: psycopg.Connection, seed: dict[str, str]) -> None:
    version = _fresh_version(admin, seed, status="REVIEWED")
    admin.execute(
        """INSERT INTO review_actions (tenant_id, version_id, actor_id, action)
           VALUES (%s, %s, %s, 'SUBMIT')""",
        (seed["tenant_a"], version, seed["user_a"]),
    )
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        admin.execute("UPDATE review_actions SET reason = '篡改' WHERE version_id = %s", (version,))


def test_review_actions_cannot_be_deleted(admin: psycopg.Connection, seed: dict[str, str]) -> None:
    version = _fresh_version(admin, seed, status="REVIEWED")
    admin.execute(
        """INSERT INTO review_actions (tenant_id, version_id, actor_id, action)
           VALUES (%s, %s, %s, 'SUBMIT')""",
        (seed["tenant_a"], version, seed["user_a"]),
    )
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        admin.execute("DELETE FROM review_actions WHERE version_id = %s", (version,))


def test_revoked_is_terminal(admin: psycopg.Connection, seed: dict[str, str]) -> None:
    """撤销后不得复活；撤销必须立即生效，不能等待索引清理。"""
    version = _fresh_version(admin, seed, status="APPROVED_INTERNAL", approved_by=seed["user_a"])
    admin.execute("UPDATE knowledge_versions SET status = 'REVOKED' WHERE id = %s", (version,))
    with pytest.raises(psycopg.errors.CheckViolation):
        admin.execute("UPDATE knowledge_versions SET status = 'APPROVED_INTERNAL' WHERE id = %s", (version,))


def test_full_approval_chain_succeeds(admin: psycopg.Connection, seed: dict[str, str]) -> None:
    """正向链路：证明状态机不是把所有迁移都拒了。"""
    approver = create_principal(admin, seed["tenant_a"], f"public-approver-{uuid.uuid4().hex[:8]}@example.test")
    version = _fresh_version(admin, seed, status="RAW", classification=0, audience="customer_public")

    admin.execute("UPDATE knowledge_versions SET status = 'DRAFT' WHERE id = %s", (version,))
    admin.execute("UPDATE knowledge_versions SET status = 'REVIEWED' WHERE id = %s", (version,))
    admin.execute(
        "UPDATE knowledge_versions SET status = 'APPROVED_INTERNAL', approved_by = %s WHERE id = %s",
        (seed["user_a"], version),
    )
    admin.execute(
        """UPDATE knowledge_versions SET status = 'PUBLISHED_PUBLIC', public_approved_by = %s
            WHERE id = %s""",
        (approver, version),
    )

    row = admin.execute(
        "SELECT status, approved_by::text, public_approved_by::text FROM knowledge_versions WHERE id = %s",
        (version,),
    ).fetchone()
    assert row is not None
    assert row[0] == "PUBLISHED_PUBLIC"
    assert row[1] == seed["user_a"]
    assert row[2] == approver


def test_parent_must_belong_to_same_version(admin: psycopg.Connection, seed: dict[str, str]) -> None:
    from tests.conftest import create_acl, create_chunk

    acl = create_acl(admin, seed["tenant_a"])
    v1 = _fresh_version(admin, seed, status="DRAFT", version_no=11)
    v2 = _fresh_version(admin, seed, status="DRAFT", version_no=12)
    parent = create_chunk(admin, seed["tenant_a"], v1, acl, kind="parent")

    with pytest.raises(psycopg.errors.CheckViolation):
        create_chunk(admin, seed["tenant_a"], v2, acl, kind="child", parent_id=parent)


def test_child_cannot_be_used_as_parent(admin: psycopg.Connection, seed: dict[str, str]) -> None:
    from tests.conftest import create_acl, create_chunk

    acl = create_acl(admin, seed["tenant_a"])
    v = _fresh_version(admin, seed, status="DRAFT", version_no=21)
    parent = create_chunk(admin, seed["tenant_a"], v, acl, kind="parent")
    child = create_chunk(admin, seed["tenant_a"], v, acl, kind="child", parent_id=parent)

    with pytest.raises(psycopg.errors.CheckViolation):
        create_chunk(admin, seed["tenant_a"], v, acl, kind="child", parent_id=child)


def test_parent_chunk_cannot_have_parent(admin: psycopg.Connection, seed: dict[str, str]) -> None:
    from tests.conftest import create_acl, create_chunk

    acl = create_acl(admin, seed["tenant_a"])
    v = _fresh_version(admin, seed, status="DRAFT", version_no=31)
    parent = create_chunk(admin, seed["tenant_a"], v, acl, kind="parent")

    with pytest.raises(psycopg.errors.CheckViolation):
        create_chunk(admin, seed["tenant_a"], v, acl, kind="parent", parent_id=parent)
