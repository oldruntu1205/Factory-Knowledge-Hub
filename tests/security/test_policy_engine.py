"""ACL 判定引擎（应用层第一道防线）。

核心断言是三个"不得放行"：显式 Deny 不得被 Allow 覆盖、无匹配不得放行、
策略缺失不得当作公开。这三条任何一条失效，后面所有 RLS 测试都失去意义。
"""

from __future__ import annotations

import pytest

from fkh.authz.policy import (
    AclFormatError,
    AclPolicy,
    Decision,
    DenyReason,
    Subject,
    evaluate,
)

pytestmark = pytest.mark.security


def _subject(**overrides: object) -> Subject:
    base: dict[str, object] = {
        "principal_id": "user-1",
        "tenant_id": "tenant-1",
        "roles": frozenset({"staff"}),
        "groups": frozenset(),
        "departments": frozenset({"tech"}),
    }
    base.update(overrides)
    return Subject(**base)  # type: ignore[arg-type]


def test_allow_by_role() -> None:
    policy = AclPolicy.from_json({"allow": {"roles": ["staff"]}})
    decision, reason = evaluate(policy, _subject())
    assert decision is Decision.ALLOW
    assert reason is None


def test_allow_by_department() -> None:
    policy = AclPolicy.from_json({"allow": {"departments": ["tech"]}})
    assert evaluate(policy, _subject())[0] is Decision.ALLOW


def test_allow_by_subject_id() -> None:
    policy = AclPolicy.from_json({"allow": {"subjects": ["user-1"]}})
    assert evaluate(policy, _subject())[0] is Decision.ALLOW


def test_deny_beats_allow() -> None:
    """同一主体同时命中 allow 与 deny 时，必须拒绝。"""
    policy = AclPolicy.from_json({"allow": {"roles": ["staff"]}, "deny": {"roles": ["staff"]}})
    decision, reason = evaluate(policy, _subject())
    assert decision is Decision.DENY
    assert reason is DenyReason.EXPLICIT_DENY


def test_deny_by_department_beats_role_allow() -> None:
    policy = AclPolicy.from_json({"allow": {"roles": ["staff"]}, "deny": {"departments": ["tech"]}})
    assert evaluate(policy, _subject())[0] is Decision.DENY


def test_no_match_denies_by_default() -> None:
    policy = AclPolicy.from_json({"allow": {"roles": ["finance"]}})
    decision, reason = evaluate(policy, _subject())
    assert decision is Decision.DENY
    assert reason is DenyReason.NO_MATCH


def test_empty_allow_denies_everyone() -> None:
    """空 allow 表示无人可读，不得被解释为公开。"""
    policy = AclPolicy.from_json({})
    decision, reason = evaluate(policy, _subject())
    assert decision is Decision.DENY
    assert reason is DenyReason.NO_MATCH


def test_missing_policy_denies() -> None:
    decision, reason = evaluate(None, _subject())
    assert decision is Decision.DENY
    assert reason is DenyReason.MISSING_POLICY


def test_cross_tenant_denied_even_when_policy_matches() -> None:
    """策略匹配也不能替代租户校验，否则复制一份策略即可跨租户读取。"""
    policy = AclPolicy.from_json({"allow": {"roles": ["staff"]}})
    decision, reason = evaluate(policy, _subject(tenant_id="tenant-2"), resource_tenant_id="tenant-1")
    assert decision is Decision.DENY
    assert reason is DenyReason.CROSS_TENANT


def test_unknown_top_level_key_rejected() -> None:
    with pytest.raises(AclFormatError, match="未知顶层键"):
        AclPolicy.from_json({"allow": {"roles": ["staff"]}, "public": True})


def test_unknown_dimension_rejected() -> None:
    with pytest.raises(AclFormatError, match="未知维度"):
        AclPolicy.from_json({"allow": {"role": ["staff"]}})


def test_string_instead_of_list_rejected() -> None:
    """单值字符串是常见手写错误，必须报错而不是当成单元素集合。"""
    with pytest.raises(AclFormatError, match="必须是字符串数组"):
        AclPolicy.from_json({"allow": {"roles": "staff"}})


def test_non_object_policy_rejected() -> None:
    with pytest.raises(AclFormatError, match="必须是对象"):
        AclPolicy.from_json(["staff"])


def test_blank_subject_rejected() -> None:
    with pytest.raises(AclFormatError, match="空值"):
        AclPolicy.from_json({"allow": {"subjects": ["  "]}})


def test_fragment_equal_to_parent_is_not_widened() -> None:
    parent = AclPolicy.from_json({"allow": {"roles": ["staff", "tech"]}})
    child = AclPolicy.from_json({"allow": {"roles": ["staff"]}})
    assert child.widened_dimensions(parent) == ()


def test_fragment_with_extra_role_is_widened() -> None:
    parent = AclPolicy.from_json({"allow": {"roles": ["staff"]}})
    child = AclPolicy.from_json({"allow": {"roles": ["staff", "contractor"]}})
    assert "allow.roles" in child.widened_dimensions(parent)


def test_fragment_dropping_parent_deny_is_widened() -> None:
    """父文档禁止的角色，子块不得通过省略 deny 把它放回来。"""
    parent = AclPolicy.from_json({"allow": {"roles": ["staff"]}, "deny": {"roles": ["intern"]}})
    child = AclPolicy.from_json({"allow": {"roles": ["staff"]}})
    assert "deny.roles" in child.widened_dimensions(parent)
