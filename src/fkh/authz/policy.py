"""应用层 ACL 判定（第一道防线）。

对应 L1 §3.4：Deny 优先于 Allow、缺失 ACL 拒绝、跨租户默认拒绝、
知识片段权限不得宽于来源父文档。

设计取舍：
- 不提供 `*` 通配符。通配符会把"写错一个字符"放大成全员可见，
  而策略表由人工维护，缺省拒绝比便利更重要。
- 解析失败一律抛错，不允许降级为空策略。jsonb 是外部输入，
  格式漂移必须显式暴露，而不是静默放行。
- tenant_id 参与判定。跨租户直接拒绝，不依赖数据库 RLS 兜底。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

DIMENSIONS: tuple[str, ...] = ("roles", "groups", "departments", "subjects")


class AclFormatError(ValueError):
    """策略结构不符合约定。出现即拒绝，不做容错补救。"""


class Decision(StrEnum):
    ALLOW = "ALLOW"
    DENY = "DENY"


class DenyReason(StrEnum):
    EXPLICIT_DENY = "EXPLICIT_DENY"
    NO_MATCH = "NO_MATCH"
    MISSING_POLICY = "MISSING_POLICY"
    CROSS_TENANT = "CROSS_TENANT"


@dataclass(frozen=True, slots=True)
class Subject:
    """请求主体。属性全部来自已验证令牌，不接受客户端自报。"""

    principal_id: str
    tenant_id: str
    roles: frozenset[str] = frozenset()
    groups: frozenset[str] = frozenset()
    departments: frozenset[str] = frozenset()

    def attribute(self, dimension: str) -> frozenset[str]:
        if dimension == "roles":
            return self.roles
        if dimension == "groups":
            return self.groups
        if dimension == "departments":
            return self.departments
        if dimension == "subjects":
            return frozenset({self.principal_id})
        raise AclFormatError(f"未知的 ACL 维度：{dimension}")


@dataclass(frozen=True, slots=True)
class AclPolicy:
    """策略四元组。空 allow 表示无人可读，不是"人人可读"。"""

    allow_roles: frozenset[str] = frozenset()
    allow_groups: frozenset[str] = frozenset()
    allow_departments: frozenset[str] = frozenset()
    allow_subjects: frozenset[str] = frozenset()
    deny_roles: frozenset[str] = frozenset()
    deny_groups: frozenset[str] = frozenset()
    deny_departments: frozenset[str] = frozenset()
    deny_subjects: frozenset[str] = frozenset()

    # ---- 解析 -------------------------------------------------------------
    @classmethod
    def from_json(cls, payload: Any) -> AclPolicy:
        if not isinstance(payload, Mapping):
            raise AclFormatError(f"ACL 策略必须是对象，实际为 {type(payload).__name__}")

        unknown = set(payload) - {"allow", "deny"}
        if unknown:
            raise AclFormatError(f"ACL 策略含未知顶层键：{sorted(unknown)}")

        allow = _parse_section(payload.get("allow"), "allow")
        deny = _parse_section(payload.get("deny"), "deny")

        return cls(
            allow_roles=allow["roles"],
            allow_groups=allow["groups"],
            allow_departments=allow["departments"],
            allow_subjects=allow["subjects"],
            deny_roles=deny["roles"],
            deny_groups=deny["groups"],
            deny_departments=deny["departments"],
            deny_subjects=deny["subjects"],
        )

    # ---- 判定 -------------------------------------------------------------
    def _allow_sets(self) -> dict[str, frozenset[str]]:
        return {
            "roles": self.allow_roles,
            "groups": self.allow_groups,
            "departments": self.allow_departments,
            "subjects": self.allow_subjects,
        }

    def _deny_sets(self) -> dict[str, frozenset[str]]:
        return {
            "roles": self.deny_roles,
            "groups": self.deny_groups,
            "departments": self.deny_departments,
            "subjects": self.deny_subjects,
        }

    def allows(self, subject: Subject) -> bool:
        allow = self._allow_sets()
        return any(allow[d] & subject.attribute(d) for d in DIMENSIONS)

    def denies(self, subject: Subject) -> bool:
        deny = self._deny_sets()
        return any(deny[d] & subject.attribute(d) for d in DIMENSIONS)

    def widened_dimensions(self, parent: AclPolicy) -> tuple[str, ...]:
        """返回本策略相对父策略被放宽的维度。

        采用集合包含的保守判定：子块的 allow 不得超出父块，deny 不得少于父块。
        保守意味着可能拒绝掉事实上等价的策略，需要人工复核；
        反方向（漏判放宽）会直接造成越权，不可接受。
        """
        issues: list[str] = []
        mine_allow, parent_allow = self._allow_sets(), parent._allow_sets()
        mine_deny, parent_deny = self._deny_sets(), parent._deny_sets()
        for dimension in DIMENSIONS:
            if not mine_allow[dimension] <= parent_allow[dimension]:
                issues.append(f"allow.{dimension}")
            if not parent_deny[dimension] <= mine_deny[dimension]:
                issues.append(f"deny.{dimension}")
        return tuple(issues)


def _parse_section(raw: Any, name: str) -> dict[str, frozenset[str]]:
    empty: dict[str, frozenset[str]] = {dimension: frozenset() for dimension in DIMENSIONS}
    if raw is None:
        return empty
    if not isinstance(raw, Mapping):
        raise AclFormatError(f"ACL.{name} 必须是对象，实际为 {type(raw).__name__}")

    unknown = set(raw) - set(DIMENSIONS)
    if unknown:
        raise AclFormatError(f"ACL.{name} 含未知维度：{sorted(unknown)}")

    parsed: dict[str, frozenset[str]] = {}
    for dimension in DIMENSIONS:
        values = raw.get(dimension, [])
        if isinstance(values, str) or not isinstance(values, list | tuple | set | frozenset):
            raise AclFormatError(f"ACL.{name}.{dimension} 必须是字符串数组")
        if any(not isinstance(item, str) or not item.strip() for item in values):
            raise AclFormatError(f"ACL.{name}.{dimension} 含空值或非字符串元素")
        parsed[dimension] = frozenset(values)
    return parsed


def evaluate(
    policy: AclPolicy | None,
    subject: Subject,
    *,
    resource_tenant_id: str | None = None,
) -> tuple[Decision, DenyReason | None]:
    """单条策略判定。

    resource_tenant_id 传入时先做租户比对：跨租户直接拒绝，
    不进入 allow/deny 计算，避免"策略恰好匹配"导致的越权。
    """
    if resource_tenant_id is not None and resource_tenant_id != subject.tenant_id:
        return Decision.DENY, DenyReason.CROSS_TENANT
    if policy is None:
        return Decision.DENY, DenyReason.MISSING_POLICY
    if policy.denies(subject):
        return Decision.DENY, DenyReason.EXPLICIT_DENY
    if policy.allows(subject):
        return Decision.ALLOW, None
    return Decision.DENY, DenyReason.NO_MATCH
