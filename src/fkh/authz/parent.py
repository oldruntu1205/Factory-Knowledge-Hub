"""父块恢复与片段权限收窄。

对应 L1 §3.4 与 §4.4-5：检索命中 child 后可以恢复 parent 全文，但：
1. parent 自身必须对当前主体可见；
2. parent 下的**全部** child 都必须对当前主体可见，否则 parent 全文会带出未授权内容；
3. 片段权限不得宽于父文档，出现即视为治理配置错误。

第 2 点是实现中最容易漏的一项：只检查"命中的那几个 child"无法证明整体可恢复，
因为未命中的 child 同样存在于 parent 文本里。因此本模块要求调用方传入
该 parent 下所有 child 的完整清单，而不是命中子集。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum

from fkh.authz.policy import AclPolicy, Decision, Subject, evaluate


class AclWideningError(ValueError):
    """片段权限宽于父文档。必须在入库前拦截，不能留到检索时补救。"""


class RestoreVerdict(StrEnum):
    ALLOWED = "ALLOWED"
    PARENT_DENIED = "PARENT_DENIED"
    CHILD_DENIED = "CHILD_DENIED"
    ACL_MISSING = "ACL_MISSING"
    CROSS_TENANT = "CROSS_TENANT"


@dataclass(frozen=True, slots=True)
class ChunkAcl:
    """切片及其权限视图。policy 为 None 表示缺少 ACL 配置。"""

    chunk_id: str
    tenant_id: str
    parent_chunk_id: str | None
    policy: AclPolicy | None


@dataclass(frozen=True, slots=True)
class RestoreDecision:
    parent_chunk_id: str
    verdict: RestoreVerdict
    denied_child_id: str | None = None

    @property
    def allowed(self) -> bool:
        return self.verdict is RestoreVerdict.ALLOWED


@dataclass(frozen=True, slots=True)
class RestorePlan:
    """检索层的最终取数计划。

    restore_parents：可整段恢复的父块，按整块使用；
    child_only：只能使用命中的 child 文本，不得扩展为父块；
    denied：当前主体完全不可用。
    """

    restore_parents: tuple[str, ...]
    child_only: tuple[str, ...]
    denied: tuple[str, ...]


def evaluate_parent_restore(
    parent: ChunkAcl,
    all_children: Sequence[ChunkAcl],
    subject: Subject,
) -> RestoreDecision:
    """判断某个父块能否整体恢复。"""
    if parent.tenant_id != subject.tenant_id:
        return RestoreDecision(parent.chunk_id, RestoreVerdict.CROSS_TENANT)

    if parent.policy is None:
        return RestoreDecision(parent.chunk_id, RestoreVerdict.ACL_MISSING)

    parent_decision, _ = evaluate(parent.policy, subject, resource_tenant_id=parent.tenant_id)
    if parent_decision is not Decision.ALLOW:
        return RestoreDecision(parent.chunk_id, RestoreVerdict.PARENT_DENIED)

    for child in all_children:
        if child.parent_chunk_id != parent.chunk_id:
            raise ValueError(f"child {child.chunk_id} 不属于 parent {parent.chunk_id}，调用方聚合有误")
        if child.tenant_id != subject.tenant_id:
            return RestoreDecision(parent.chunk_id, RestoreVerdict.CROSS_TENANT, child.chunk_id)
        if child.policy is None:
            return RestoreDecision(parent.chunk_id, RestoreVerdict.ACL_MISSING, child.chunk_id)
        child_decision, _ = evaluate(child.policy, subject, resource_tenant_id=child.tenant_id)
        if child_decision is not Decision.ALLOW:
            return RestoreDecision(parent.chunk_id, RestoreVerdict.CHILD_DENIED, child.chunk_id)

    return RestoreDecision(parent.chunk_id, RestoreVerdict.ALLOWED)


def plan_restore(
    hit_children: Sequence[ChunkAcl],
    all_children_by_parent: Mapping[str, Sequence[ChunkAcl]],
    parents: Mapping[str, ChunkAcl],
    subject: Subject,
) -> RestorePlan:
    """把命中结果分成「整段恢复父块」「仅用子块」「全部拒绝」三组。

    all_children_by_parent 必须是数据库按 parent 查出的完整子块清单。
    缺失该 parent 的条目时不做整体恢复（无法证明安全），只保留 child 文本。
    """
    restore: list[str] = []
    child_only: list[str] = []
    denied: list[str] = []

    for child in hit_children:
        decision, _ = evaluate(child.policy, subject, resource_tenant_id=child.tenant_id)
        if decision is not Decision.ALLOW:
            denied.append(child.chunk_id)
            continue

        parent_id = child.parent_chunk_id
        if parent_id is None:
            child_only.append(child.chunk_id)
            continue

        parent = parents.get(parent_id)
        siblings = all_children_by_parent.get(parent_id)
        if parent is None or siblings is None:
            # 父块缺失或子块清单不完整：退回 child 文本，绝不猜测整体可恢复
            child_only.append(child.chunk_id)
            continue

        verdict = evaluate_parent_restore(parent, siblings, subject)
        if verdict.allowed:
            if parent_id not in restore:
                restore.append(parent_id)
        else:
            child_only.append(child.chunk_id)

    return RestorePlan(tuple(restore), tuple(child_only), tuple(denied))


def assert_fragment_not_wider(child: AclPolicy, parent: AclPolicy, *, chunk_id: str) -> None:
    """入库前校验：片段权限不得宽于父文档。

    放宽方向必然是配置错误，因为片段内容取自父文档；一旦入库，
    检索时无法区分"本来就该宽"和"写错了"，只能在写入侧拒绝。
    """
    widened = child.widened_dimensions(parent)
    if widened:
        raise AclWideningError(
            f"chunk {chunk_id} 的权限宽于父文档，维度：{', '.join(widened)}"
        )
