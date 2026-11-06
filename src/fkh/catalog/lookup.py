"""目录结构化查询：货号 → 规格/单价（确定性路径，不经过模型）。

三条 L1 约束（2026-11-06 裁决）：
1. **版本状态、检索范围、ACL 都是必要条件**：状态合法但 ACL 不通过，同样不可见，
   且对外表现与"不存在"一致（不泄露条目存在性）；
2. **跨版本冲突不任取**：同一货号在多个可检索版本里对应不同事实时，判歧义并拒答；
3. **结构化必须与原文一致**：引用块正文里若不含该原文行，说明抽取或数据出错，
   不得回答（转人工复核），不能拿结构化值硬填。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

import psycopg

from fkh.authz.policy import AclPolicy, Decision, Subject, evaluate

VISIBLE_STATUSES = ("APPROVED_INTERNAL", "PUBLISHED_PUBLIC")


@dataclass(frozen=True, slots=True)
class CatalogMatch:
    code: str
    spec: str
    raw_text: str
    raw_price: str
    price: Any
    currency: str
    chunk_id: UUID
    version_id: UUID
    document_id: UUID
    version_no: int
    locator: dict[str, Any]
    chunk_text: str


@dataclass(frozen=True, slots=True)
class CatalogLookup:
    """一次查询的结果：命中 / 歧义 / 无。"""

    match: CatalogMatch | None = None
    ambiguous: bool = False
    conflict_count: int = 0

    @property
    def found(self) -> bool:
        return self.match is not None


def _policy(payload: Any) -> AclPolicy | None:
    if payload is None:
        return None
    try:
        return AclPolicy.from_json(payload)
    except Exception:  # noqa: BLE001 - 策略损坏按"无策略"处理，宁可拒绝
        return None


def find_catalog_item(
    conn: psycopg.Connection[Any],
    tenant_id: UUID,
    code: str,
    *,
    knowledge_scope: str,
    subject: Subject,
) -> CatalogLookup:
    """按货号查当前可检索的条目。

    不可见（不存在/版本不可见/ACL 拒绝）统一返回未命中——调用方无法据此探测存在性。
    """
    rows = conn.execute(
        """SELECT ci.code, ci.spec, ci.raw_text, ci.raw_price, ci.price, ci.currency,
                  ci.chunk_id, ci.version_id, v.document_id, v.version_no, ci.locator,
                  c.chunk_text, a.policy_json
             FROM catalog_items ci
             JOIN knowledge_versions v
               ON v.tenant_id = ci.tenant_id AND v.id = ci.version_id
             JOIN knowledge_chunks c
               ON c.tenant_id = ci.tenant_id AND c.id = ci.chunk_id
             LEFT JOIN acl_policies a
               ON a.tenant_id = c.tenant_id AND a.id = c.acl_policy_id
            WHERE ci.tenant_id = %s
              AND ci.code = %s
              AND v.status = ANY(%s)
              AND v.required_scope = %s
              AND (v.effective_at IS NULL OR v.effective_at <= now())
              AND (v.expires_at IS NULL OR v.expires_at > now())
            ORDER BY v.version_no DESC, ci.created_at DESC""",
        (tenant_id, code, list(VISIBLE_STATUSES), knowledge_scope),
    ).fetchall()

    visible: list[tuple[Any, ...]] = []
    for row in rows:
        decision, _ = evaluate(_policy(row[12]), subject, resource_tenant_id=str(tenant_id))
        if decision is not Decision.ALLOW:
            continue
        visible.append(row)

    if not visible:
        return CatalogLookup()

    # 歧义判定：不同版本给出不同（规格, 价格）→ 拒答，不任取一条
    facts = {(str(row[1]), str(row[3]), str(row[4])) for row in visible}
    if len(facts) > 1:
        return CatalogLookup(ambiguous=True, conflict_count=len(facts))

    row = visible[0]  # 已按版本号降序：同事实取最新版本，作为引用来源
    return CatalogLookup(
        match=CatalogMatch(
            code=str(row[0]),
            spec=str(row[1]),
            raw_text=str(row[2]),
            raw_price=str(row[3]),
            price=row[4],
            currency=str(row[5]),
            chunk_id=UUID(str(row[6])),
            version_id=UUID(str(row[7])),
            document_id=UUID(str(row[8])),
            version_no=int(row[9]),
            locator=dict(row[10]) if row[10] else {},
            chunk_text=str(row[11]),
        )
    )


def chunk_contains_row(chunk_text: str, raw_text: str) -> bool:
    """结构化事实必须能在原文块里找到（抽取一致性校验）。"""
    return raw_text.strip() in chunk_text


__all__ = [
    "CatalogLookup",
    "CatalogMatch",
    "VISIBLE_STATUSES",
    "chunk_contains_row",
    "find_catalog_item",
]
