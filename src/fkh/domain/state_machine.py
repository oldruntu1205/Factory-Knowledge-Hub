"""知识版本状态机（应用层镜像）。

与 migrations/0001_core_schema.sql 中的 `fkh_guard_version_mutation` 触发器保持同一张
迁移表。两层重复是刻意的：应用层给出可读错误并在事务前拦截，数据库层作为第二道防线，
防止绕过服务直接改库。

一致性由 tests/security/test_state_machine_parity.py 用 7×7 全组合验证 ——
任何一侧改动而另一侧没跟上，测试会立刻失败。
"""

from __future__ import annotations

from enum import StrEnum


class VersionStatus(StrEnum):
    RAW = "RAW"
    DRAFT = "DRAFT"
    REVIEWED = "REVIEWED"
    APPROVED_INTERNAL = "APPROVED_INTERNAL"
    PUBLISHED_PUBLIC = "PUBLISHED_PUBLIC"
    SUPERSEDED = "SUPERSEDED"
    REVOKED = "REVOKED"
    # 从未公开发布、主动作废（2026-11-05 L1 裁决 B）。与 REVOKED 的区别是审计语义：
    # DISCARDED 的版本从未对外可见；REVOKED 是曾发布/批准后撤回。
    DISCARDED = "DISCARDED"


class ReviewAction(StrEnum):
    """对应 review_actions.action 的取值。"""

    SUBMIT = "SUBMIT"
    REVIEW_PASS = "REVIEW_PASS"
    REVIEW_REJECT = "REVIEW_REJECT"
    APPROVE_INTERNAL = "APPROVE_INTERNAL"
    APPROVE_PUBLIC = "APPROVE_PUBLIC"
    PUBLISH = "PUBLISH"
    SUPERSEDE = "SUPERSEDE"
    REVOKE = "REVOKE"
    DISCARD = "DISCARD"


class IllegalTransition(ValueError):
    pass


# (源状态, 目标状态) -> 触发动作。键集合即白名单，顺序无关。
_TRANSITIONS: dict[tuple[VersionStatus, VersionStatus], ReviewAction] = {
    (VersionStatus.RAW, VersionStatus.DRAFT): ReviewAction.SUBMIT,
    (VersionStatus.DRAFT, VersionStatus.REVIEWED): ReviewAction.REVIEW_PASS,
    (VersionStatus.REVIEWED, VersionStatus.DRAFT): ReviewAction.REVIEW_REJECT,
    (VersionStatus.REVIEWED, VersionStatus.APPROVED_INTERNAL): ReviewAction.APPROVE_INTERNAL,
    (VersionStatus.APPROVED_INTERNAL, VersionStatus.DRAFT): ReviewAction.REVIEW_REJECT,
    (VersionStatus.APPROVED_INTERNAL, VersionStatus.PUBLISHED_PUBLIC): ReviewAction.PUBLISH,
    (VersionStatus.APPROVED_INTERNAL, VersionStatus.SUPERSEDED): ReviewAction.SUPERSEDE,
    (VersionStatus.APPROVED_INTERNAL, VersionStatus.REVOKED): ReviewAction.REVOKE,
    (VersionStatus.PUBLISHED_PUBLIC, VersionStatus.SUPERSEDED): ReviewAction.SUPERSEDE,
    (VersionStatus.PUBLISHED_PUBLIC, VersionStatus.REVOKED): ReviewAction.REVOKE,
    (VersionStatus.SUPERSEDED, VersionStatus.REVOKED): ReviewAction.REVOKE,
    # 弃用：只允许从未对外发布的状态进入 DISCARDED；PUBLISHED_PUBLIC / SUPERSEDED 必须走 REVOKED
    (VersionStatus.RAW, VersionStatus.DISCARDED): ReviewAction.DISCARD,
    (VersionStatus.DRAFT, VersionStatus.DISCARDED): ReviewAction.DISCARD,
    (VersionStatus.REVIEWED, VersionStatus.DISCARDED): ReviewAction.DISCARD,
    (VersionStatus.APPROVED_INTERNAL, VersionStatus.DISCARDED): ReviewAction.DISCARD,
}

# 终态集合：撤销与弃用都不可再迁移。保留 TERMINAL_STATUS 作为历史别名（REVOKED）以兼容旧调用。
TERMINAL_STATUS = VersionStatus.REVOKED
TERMINAL_STATUSES = frozenset({VersionStatus.REVOKED, VersionStatus.DISCARDED})


def can_transition(source: VersionStatus, target: VersionStatus) -> bool:
    return (source, target) in _TRANSITIONS


def required_action(source: VersionStatus, target: VersionStatus) -> ReviewAction:
    try:
        return _TRANSITIONS[(source, target)]
    except KeyError as exc:
        raise IllegalTransition(f"非法状态迁移：{source} -> {target}") from exc


def assert_transition(source: VersionStatus, target: VersionStatus) -> ReviewAction:
    if source is target:
        raise IllegalTransition(f"状态未变化：{source}，无需迁移")
    return required_action(source, target)


def allowed_targets(source: VersionStatus) -> frozenset[VersionStatus]:
    return frozenset(target for (origin, target) in _TRANSITIONS if origin is source)


def assert_publishable(status: VersionStatus, *, classification: int, audience: str) -> None:
    """对外发布的前置条件（L1 §3.2）。

    数据库侧有 CHECK 与触发器双重约束，这里提前给出可读原因。
    """
    if status is not VersionStatus.APPROVED_INTERNAL:
        raise IllegalTransition(f"只有 APPROVED_INTERNAL 可发布，当前为 {status}")
    if classification != 0:
        raise IllegalTransition(f"C{classification} 内容不得对外发布，仅 C0 可公开")
    if audience != "customer_public":
        raise IllegalTransition("对外发布要求 audience=customer_public")
