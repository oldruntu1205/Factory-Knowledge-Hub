"""父块恢复与混合权限隔离（P0）。

对应 L1 §8.4：parent 混合了可公开和内部段落时，不能因为某个公开 child 命中
就把整个 parent 恢复出来。本文件用纯逻辑覆盖判定，末尾用数据库真实 jsonb
验证代码与迁移约定一致。
"""

from __future__ import annotations

import psycopg
import pytest

from fkh.authz.parent import (
    AclWideningError,
    ChunkAcl,
    RestoreVerdict,
    assert_fragment_not_wider,
    evaluate_parent_restore,
    plan_restore,
)
from fkh.authz.policy import AclPolicy, Subject, evaluate

pytestmark = pytest.mark.security

TENANT = "tenant-1"


def _subject(**overrides: object) -> Subject:
    base: dict[str, object] = {
        "principal_id": "user-1",
        "tenant_id": TENANT,
        "roles": frozenset({"staff"}),
        "groups": frozenset(),
        "departments": frozenset({"tech"}),
    }
    base.update(overrides)
    return Subject(**base)  # type: ignore[arg-type]


def _policy(**sections: object) -> AclPolicy:
    return AclPolicy.from_json(dict(sections))


def _chunk(
    chunk_id: str,
    *,
    parent: str | None = None,
    tenant: str = TENANT,
    allow_role: str | None = "staff",
) -> ChunkAcl:
    policy = AclPolicy.from_json({"allow": {"roles": [allow_role]}}) if allow_role else None
    return ChunkAcl(chunk_id=chunk_id, tenant_id=tenant, parent_chunk_id=parent, policy=policy)


def _chunk_without_acl(
    chunk_id: str, *, parent: str | None = None, tenant: str = TENANT
) -> ChunkAcl:
    """模拟 ACL 配置缺失：数据库中 acl_policy_id 非空，但策略内容未落地。"""
    return ChunkAcl(chunk_id=chunk_id, tenant_id=tenant, parent_chunk_id=parent, policy=None)


# ---------------------------------------------------------------------------
# evaluate_parent_restore
# ---------------------------------------------------------------------------
def test_parent_restore_allowed_when_all_children_visible() -> None:
    parent = _chunk("p1")
    children = [_chunk("c1", parent="p1"), _chunk("c2", parent="p1")]
    decision = evaluate_parent_restore(parent, children, _subject())
    assert decision.allowed
    assert decision.verdict is RestoreVerdict.ALLOWED


def test_parent_restore_denied_when_one_child_restricted() -> None:
    parent = _chunk("p1")
    children = [
        _chunk("c1", parent="p1"),
        _chunk("c2", parent="p1", allow_role="exec"),
    ]
    decision = evaluate_parent_restore(parent, children, _subject())
    assert not decision.allowed
    assert decision.verdict is RestoreVerdict.CHILD_DENIED
    assert decision.denied_child_id == "c2"


def test_parent_restore_denied_when_parent_restricted() -> None:
    parent = _chunk("p1", allow_role="exec")
    decision = evaluate_parent_restore(parent, [_chunk("c1", parent="p1")], _subject())
    assert decision.verdict is RestoreVerdict.PARENT_DENIED


def test_parent_restore_denied_when_parent_policy_missing() -> None:
    parent = _chunk_without_acl("p1")
    decision = evaluate_parent_restore(parent, [_chunk("c1", parent="p1")], _subject())
    assert decision.verdict is RestoreVerdict.ACL_MISSING


def test_parent_restore_denied_when_child_policy_missing() -> None:
    parent = _chunk("p1")
    decision = evaluate_parent_restore(parent, [_chunk_without_acl("c1", parent="p1")], _subject())
    assert decision.verdict is RestoreVerdict.ACL_MISSING


def test_parent_restore_cross_tenant() -> None:
    parent = _chunk("p1", tenant="tenant-2")
    decision = evaluate_parent_restore(parent, [], _subject())
    assert decision.verdict is RestoreVerdict.CROSS_TENANT


def test_child_from_other_parent_is_programming_error() -> None:
    """传错子块属于调用方聚合错误，必须抛错而不是给出一个看似合理的结果。"""
    parent = _chunk("p1")
    with pytest.raises(ValueError, match="不属于"):
        evaluate_parent_restore(parent, [_chunk("c9", parent="p2")], _subject())


# ---------------------------------------------------------------------------
# plan_restore
# ---------------------------------------------------------------------------
def test_plan_restores_parent_when_everything_visible() -> None:
    parent = _chunk("p1")
    siblings = [_chunk("c1", parent="p1"), _chunk("c2", parent="p1")]
    plan = plan_restore([siblings[0]], {"p1": siblings}, {"p1": parent}, _subject())
    assert plan.restore_parents == ("p1",)
    assert plan.child_only == ()


def test_plan_downgrades_when_unhit_sibling_is_restricted() -> None:
    """命中 c1，但同一 parent 下的 c2 不可见：只能返回 c1 文本。"""
    parent = _chunk("p1")
    hit = _chunk("c1", parent="p1")
    hidden = _chunk("c2", parent="p1", allow_role="exec")
    plan = plan_restore([hit], {"p1": [hit, hidden]}, {"p1": parent}, _subject())
    assert plan.restore_parents == ()
    assert plan.child_only == ("c1",)


def test_plan_denies_invisible_child() -> None:
    restricted = _chunk("c1", parent="p1", allow_role="exec")
    plan = plan_restore([restricted], {"p1": [restricted]}, {"p1": _chunk("p1")}, _subject())
    assert plan.denied == ("c1",)


def test_plan_without_parent_row_uses_child_only() -> None:
    hit = _chunk("c1", parent="p1")
    plan = plan_restore([hit], {"p1": [hit]}, {}, _subject())
    assert plan.restore_parents == ()
    assert plan.child_only == ("c1",)


def test_plan_without_complete_sibling_list_uses_child_only() -> None:
    """拿不到完整子块清单就无法证明整体安全，宁可降级。"""
    hit = _chunk("c1", parent="p1")
    plan = plan_restore([hit], {}, {"p1": _chunk("p1")}, _subject())
    assert plan.child_only == ("c1",)


def test_plan_root_child_without_parent_is_child_only() -> None:
    hit = _chunk("c1", parent=None)
    plan = plan_restore([hit], {}, {}, _subject())
    assert plan.child_only == ("c1",)


def test_plan_deduplicates_parent() -> None:
    parent = _chunk("p1")
    siblings = [_chunk("c1", parent="p1"), _chunk("c2", parent="p1")]
    plan = plan_restore([siblings[0], siblings[1]], {"p1": siblings}, {"p1": parent}, _subject())
    assert plan.restore_parents == ("p1",)


def test_plan_mixed_parents() -> None:
    p_ok, p_mixed = _chunk("p-ok"), _chunk("p-mixed")
    c_ok = _chunk("c-ok", parent="p-ok")
    c_hit = _chunk("c-hit", parent="p-mixed")
    c_hidden = _chunk("c-hidden", parent="p-mixed", allow_role="exec")

    plan = plan_restore(
        [c_ok, c_hit],
        {"p-ok": [c_ok], "p-mixed": [c_hit, c_hidden]},
        {"p-ok": p_ok, "p-mixed": p_mixed},
        _subject(),
    )
    assert plan.restore_parents == ("p-ok",)
    assert plan.child_only == ("c-hit",)


# ---------------------------------------------------------------------------
# 片段权限收窄
# ---------------------------------------------------------------------------
def test_fragment_narrower_than_parent_passes() -> None:
    parent = _policy(allow={"roles": ["staff", "tech"]})
    child = _policy(allow={"roles": ["staff"]})
    assert_fragment_not_wider(child, parent, chunk_id="c1")


def test_fragment_wider_than_parent_rejected() -> None:
    parent = _policy(allow={"roles": ["staff"]})
    child = _policy(allow={"roles": ["staff", "contractor"]})
    with pytest.raises(AclWideningError, match="宽于父文档"):
        assert_fragment_not_wider(child, parent, chunk_id="c1")


# ---------------------------------------------------------------------------
# 与数据库约定的一致性
# ---------------------------------------------------------------------------
@pytest.mark.requires_pg
def test_database_policy_json_is_parseable(admin: psycopg.Connection, seed: dict[str, str]) -> None:
    """迁移注释里约定的 policy_json 结构必须能被运行时解析器读取。"""
    row = admin.execute(
        "SELECT policy_json FROM acl_policies WHERE id = %s", (seed["acl_a"],)
    ).fetchone()
    assert row is not None

    policy = AclPolicy.from_json(row[0])
    subject = _subject(tenant_id=seed["tenant_a"])
    decision, _ = evaluate(policy, subject, resource_tenant_id=seed["tenant_a"])
    assert decision.value == "ALLOW"


@pytest.mark.requires_pg
def test_database_rejects_unknown_shape_at_runtime(admin: psycopg.Connection, seed: dict[str, str]) -> None:
    """结构非法的策略可以被 jsonb 接受，但必须在应用层被拒绝。"""
    from fkh.authz.policy import AclFormatError

    malformed = {"allow": {"role": ["staff"]}}
    with pytest.raises(AclFormatError):
        AclPolicy.from_json(malformed)
