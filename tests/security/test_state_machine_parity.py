"""应用层状态机与数据库触发器的一致性（P0）。

两层各写一份迁移表是刻意的冗余。冗余的代价是可能不同步，所以用 7×7 全组合
强制对齐：任何一侧改了白名单而另一侧没跟上，这里立刻失败。

同状态组合被排除：它不构成迁移，数据库对 "UPDATE 成同一个值" 不做白名单检查，
拿它比较没有意义。
"""

from __future__ import annotations

import itertools

import psycopg
import pytest
from tests.conftest import create_principal, create_version

from fkh.domain.state_machine import (
    IllegalTransition,
    ReviewAction,
    VersionStatus,
    allowed_targets,
    assert_publishable,
    assert_transition,
    can_transition,
    required_action,
)

pytestmark = [pytest.mark.security, pytest.mark.requires_pg]

_version_seq = itertools.count(20000)

ALL_STATUSES = list(VersionStatus)
PAIRS = [(source, target) for source in ALL_STATUSES for target in ALL_STATUSES if source != target]


def _make_version(admin: psycopg.Connection, seed: dict[str, str], status: VersionStatus) -> str:
    """按目标源状态造一个合法版本。

    PUBLISHED_PUBLIC 在数据库层还要求 C0 + customer_public + 两个不同审批人，
    所以造数时必须一并满足，否则测的是 CHECK 约束而不是状态白名单。
    """
    if status is VersionStatus.PUBLISHED_PUBLIC:
        approver = create_principal(admin, seed["tenant_a"], f"publisher-{next(_version_seq)}@example.test")
        return create_version(
            admin,
            seed["tenant_a"],
            seed["doc_a"],
            status=status.value,
            version_no=next(_version_seq),
            classification=0,
            audience="customer_public",
            approved_by=seed["user_a"],
            public_approved_by=approver,
        )

    if status is VersionStatus.APPROVED_INTERNAL:
        # 用 C0 是为了让后续 (APPROVED_INTERNAL -> PUBLISHED_PUBLIC) 在数据库侧可通过，
        # 否则失败原因会是分类不满足，而不是迁移被拒
        return create_version(
            admin,
            seed["tenant_a"],
            seed["doc_a"],
            status=status.value,
            version_no=next(_version_seq),
            classification=0,
            audience="customer_public",
            approved_by=seed["user_a"],
        )

    return create_version(
        admin,
        seed["tenant_a"],
        seed["doc_a"],
        status=status.value,
        version_no=next(_version_seq),
    )


def _db_allows(admin: psycopg.Connection, version_id: str, target: VersionStatus, seed: dict[str, str]) -> bool:
    try:
        with admin.transaction():
            if target is VersionStatus.PUBLISHED_PUBLIC:
                approver = create_principal(
                    admin, seed["tenant_a"], f"publisher-{next(_version_seq)}@example.test"
                )
                admin.execute(
                    "UPDATE knowledge_versions SET status = %s, public_approved_by = %s WHERE id = %s",
                    (target.value, approver, version_id),
                )
            elif target is VersionStatus.DRAFT:
                # 退回草稿必须清空审批人，否则触发器会判定"DRAFT 带审批人"并拒绝。
                # 这是业务语义，不是测试特例：未审草稿不应保留批准痕迹。
                admin.execute(
                    """UPDATE knowledge_versions
                          SET status = %s, approved_by = NULL, public_approved_by = NULL
                        WHERE id = %s""",
                    (target.value, version_id),
                )
            else:
                admin.execute(
                    "UPDATE knowledge_versions SET status = %s WHERE id = %s",
                    (target.value, version_id),
                )
    except psycopg.errors.CheckViolation:
        return False
    return True


@pytest.mark.parametrize(("source", "target"), PAIRS, ids=[f"{s.value}->{t.value}" for s, t in PAIRS])
def test_db_matches_python_whitelist(
    admin: psycopg.Connection, seed: dict[str, str], source: VersionStatus, target: VersionStatus
) -> None:
    version_id = _make_version(admin, seed, source)
    expected = can_transition(source, target)
    actual = _db_allows(admin, version_id, target, seed)
    assert actual is expected, f"{source} -> {target}: 数据库={actual} 应用层={expected}"


def test_python_rejects_same_state() -> None:
    for status in ALL_STATUSES:
        with pytest.raises(IllegalTransition):
            assert_transition(status, status)


def test_every_allowed_transition_has_action() -> None:
    for source in ALL_STATUSES:
        for target in allowed_targets(source):
            action = required_action(source, target)
            assert isinstance(action, ReviewAction)


def test_revoked_is_terminal() -> None:
    assert allowed_targets(VersionStatus.REVOKED) == frozenset()
    for target in ALL_STATUSES:
        if target is not VersionStatus.REVOKED:
            assert not can_transition(VersionStatus.REVOKED, target)


def test_publish_requires_c0_and_public_audience() -> None:
    with pytest.raises(IllegalTransition, match="C1"):
        assert_publishable(VersionStatus.APPROVED_INTERNAL, classification=1, audience="customer_public")
    with pytest.raises(IllegalTransition, match="audience"):
        assert_publishable(VersionStatus.APPROVED_INTERNAL, classification=0, audience="internal")
    with pytest.raises(IllegalTransition, match="APPROVED_INTERNAL"):
        assert_publishable(VersionStatus.DRAFT, classification=0, audience="customer_public")

    assert_publishable(VersionStatus.APPROVED_INTERNAL, classification=0, audience="customer_public")
