"""目录冲突待办：组级记录、人工决议与回填。

模型（2026-11-06 L1 裁决）：
- **组表是决议事实的唯一来源**：一个 (tenant, version, code) 一个组，状态 OPEN / KEEP / DROP_ALL；
- 候选行只记录"抽取到的原文行 + 来源"，不各自带决议状态，避免出现两个 KEEP；
- 决议以组为单位、一次生效、不可原地修改（错判靠新版本，不在本切片做改判入口）；
- 决议与回填在同一事务：锁组 → 校验 OPEN → 校验候选归属与版本可操作 → 写条目/断言无条目 → 落决议。

身份：决议必须由**该租户的活跃 user 主体**执行。agent/service 不得代签；`--actor` 只是声明，
CLI 会把它拿去数据库核对，伪造 UUID 或跨租户主体一律拒绝。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

# 仍可操作（可决议/可回填）的版本状态；REVOKED / DISCARDED / SUPERSEDED 一律只读
OPERABLE_STATUSES = ("RAW", "DRAFT", "REVIEWED", "APPROVED_INTERNAL", "PUBLISHED_PUBLIC")


class ConflictError(ValueError):
    """决议不合法（状态不符、候选不属于该组、身份不合格、数据不一致）。"""


class ConflictNotFound(LookupError):
    """待办组不存在，或不属于当前租户。"""


@dataclass(frozen=True, slots=True)
class ConflictCandidate:
    candidate_id: UUID
    chunk_id: UUID
    raw_text: str
    spec: str
    raw_price: str
    price: Any
    locator: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ConflictGroup:
    group_id: UUID
    version_id: UUID
    code: str
    status: str
    selected_candidate_id: UUID | None
    resolved_by: UUID | None
    note: str | None
    candidates: tuple[ConflictCandidate, ...]


def bind_tenant(conn: psycopg.Connection[Any], tenant_id: UUID) -> None:
    """绑定事务级租户上下文。

    RLS 依赖它；不设就查不到任何行（表现为"库里明明有、查询却是空"）。
    每个公开入口都显式绑定，避免调用方 commit 之后忘记重建上下文。
    """
    conn.execute("SELECT set_config('app.tenant_id', %s, true)", (str(tenant_id),))


def require_operator(conn: psycopg.Connection[Any], tenant_id: UUID, actor_id: UUID) -> None:
    """核对操作者：必须是该租户的活跃 user 主体。"""
    row = conn.execute(
        "SELECT is_active, principal_type FROM principals WHERE tenant_id = %s AND id = %s",
        (tenant_id, actor_id),
    ).fetchone()
    if row is None:
        raise ConflictError(f"操作者 {actor_id} 不属于租户 {tenant_id}")
    if not bool(row[0]):
        raise ConflictError(f"操作者 {actor_id} 已停用")
    if str(row[1]) != "user":
        raise ConflictError("只有用户主体可以做出目录决议（agent/service 不得代签）")


def _version_status(
    conn: psycopg.Connection[Any], tenant_id: UUID, version_id: UUID
) -> str:
    row = conn.execute(
        "SELECT status FROM knowledge_versions WHERE tenant_id = %s AND id = %s",
        (tenant_id, version_id),
    ).fetchone()
    if row is None:
        raise ConflictNotFound(f"版本 {version_id} 不存在或不属于该租户")
    status = str(row[0])
    if status not in OPERABLE_STATUSES:
        raise ConflictError(f"版本状态 {status} 不可再操作（撤销/弃用/被替代后只读）")
    return status


def lock_group(
    conn: psycopg.Connection[Any], tenant_id: UUID, version_id: UUID, code: str
) -> tuple[UUID, str, UUID | None] | None:
    """锁住待办组并返回 (group_id, status, selected_candidate_id)。"""
    row = conn.execute(
        """SELECT group_id, status, selected_candidate_id
             FROM catalog_conflict_groups
            WHERE tenant_id = %s AND version_id = %s AND code = %s
              FOR UPDATE""",
        (tenant_id, version_id, code),
    ).fetchone()
    if row is None:
        return None
    return UUID(str(row[0])), str(row[1]), (UUID(str(row[2])) if row[2] else None)


def create_group(
    conn: psycopg.Connection[Any], tenant_id: UUID, version_id: UUID, code: str
) -> UUID:
    row = conn.execute(
        """INSERT INTO catalog_conflict_groups (tenant_id, version_id, code)
           VALUES (%s, %s, %s)
           ON CONFLICT (tenant_id, version_id, code) DO NOTHING
           RETURNING group_id""",
        (tenant_id, version_id, code),
    ).fetchone()
    if row is not None:
        return UUID(str(row[0]))
    # 并发下另一个事务先建了：拿它并加锁
    locked = lock_group(conn, tenant_id, version_id, code)
    if locked is None:  # pragma: no cover - 只可能被删除
        raise ConflictError("待办组创建失败")
    return locked[0]


def add_candidates(
    conn: psycopg.Connection[Any],
    tenant_id: UUID,
    group_id: UUID,
    candidates: list[tuple[UUID, str, str, str, Any, dict[str, Any]]],
) -> int:
    """写入候选（幂等）；返回新增条数。candidate 元组：(chunk_id, raw_text, spec, raw_price, price, locator)。"""
    added = 0
    for chunk_id, raw_text, spec, raw_price, price, locator in candidates:
        row = conn.execute(
            """INSERT INTO catalog_conflict_candidates
                 (tenant_id, group_id, chunk_id, raw_text, spec, raw_price, price, locator)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (tenant_id, group_id, chunk_id, raw_text) DO NOTHING
               RETURNING candidate_id""",
            (tenant_id, group_id, chunk_id, raw_text, spec, raw_price, price, Jsonb(locator or {})),
        ).fetchone()
        if row is not None:
            added += 1
    return added


def group_candidates(
    conn: psycopg.Connection[Any], tenant_id: UUID, group_id: UUID
) -> tuple[ConflictCandidate, ...]:
    rows = conn.execute(
        """SELECT candidate_id, chunk_id, raw_text, spec, raw_price, price, locator
             FROM catalog_conflict_candidates
            WHERE tenant_id = %s AND group_id = %s
            ORDER BY created_at, candidate_id""",
        (tenant_id, group_id),
    ).fetchall()
    return tuple(
        ConflictCandidate(
            candidate_id=UUID(str(row[0])),
            chunk_id=UUID(str(row[1])),
            raw_text=str(row[2]),
            spec=str(row[3]),
            raw_price=str(row[4]),
            price=row[5],
            locator=dict(row[6]) if row[6] else {},
        )
        for row in rows
    )


def resolve_group(
    conn: psycopg.Connection[Any],
    tenant_id: UUID,
    *,
    version_id: UUID,
    code: str,
    actor_id: UUID,
    candidate_id: UUID | None = None,
    drop_all: bool = False,
    note: str | None = None,
) -> ConflictGroup:
    """做决议（KEEP 或 DROP_ALL）。调用方负责事务与提交。"""
    if drop_all == (candidate_id is not None):
        raise ConflictError("必须且只能二选一：--keep <candidate_id> 或 --drop-all")

    bind_tenant(conn, tenant_id)
    require_operator(conn, tenant_id, actor_id)
    _version_status(conn, tenant_id, version_id)

    locked = lock_group(conn, tenant_id, version_id, code)
    if locked is None:
        raise ConflictNotFound(f"没有待办组：version={version_id} code={code}")
    group_id, status, _ = locked
    if status != "OPEN":
        raise ConflictError(f"该待办组已决议（{status}），本切片不支持改判")

    if drop_all:
        existed = conn.execute(
            "SELECT 1 FROM catalog_items WHERE tenant_id = %s AND version_id = %s AND code = %s",
            (tenant_id, version_id, code),
        ).fetchone()
        if existed is not None:
            raise ConflictError("该货号已存在目录条目，与 DROP_ALL 冲突；请先核对数据，不静默删除")
        conn.execute(
            """UPDATE catalog_conflict_groups
                  SET status = 'DROP_ALL', resolved_by = %s, resolved_at = now(),
                      note = %s, updated_at = now(),
                      resolved_snapshot = %s
                WHERE tenant_id = %s AND group_id = %s""",
            (
                actor_id,
                note,
                Jsonb(
                    {
                        "decision": "DROP_ALL",
                        "code": code,
                        "resolved_by": str(actor_id),
                        "note": note,
                        "candidate_count": _candidate_count(conn, tenant_id, group_id),
                    }
                ),
                tenant_id,
                group_id,
            ),
        )
    else:
        row = conn.execute(
            """SELECT chunk_id, raw_text, spec, raw_price, price, locator
                 FROM catalog_conflict_candidates
                WHERE tenant_id = %s AND group_id = %s AND candidate_id = %s""",
            (tenant_id, group_id, candidate_id),
        ).fetchone()
        if row is None:
            raise ConflictError("候选不属于该待办组（或不存在）")

        chunk_id = UUID(str(row[0]))
        raw_text, spec, raw_price = str(row[1]), str(row[2]), str(row[3])
        price, locator = row[4], dict(row[5]) if row[5] else {}

        conn.execute(
            """INSERT INTO catalog_items
                 (tenant_id, version_id, chunk_id, code, spec, raw_text, raw_price,
                  price, currency, locator)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (tenant_id, version_id, code) DO NOTHING""",
            (
                tenant_id,
                version_id,
                chunk_id,
                code,
                spec,
                raw_text,
                raw_price,
                price,
                "CNY",
                Jsonb(locator),
            ),
        )
        existing = conn.execute(
            "SELECT raw_text FROM catalog_items WHERE tenant_id = %s AND version_id = %s AND code = %s",
            (tenant_id, version_id, code),
        ).fetchone()
        if existing is None or str(existing[0]) != raw_text:
            raise ConflictError("回填后条目与决议候选不一致，已回滚")

        conn.execute(
            """UPDATE catalog_conflict_groups
                  SET status = 'KEEP', selected_candidate_id = %s, resolved_by = %s,
                      resolved_at = now(), note = %s, updated_at = now(),
                      resolved_snapshot = %s
                WHERE tenant_id = %s AND group_id = %s""",
            (
                candidate_id,
                actor_id,
                note,
                Jsonb(
                    {
                        "decision": "KEEP",
                        "candidate_id": str(candidate_id),
                        "code": code,
                        "raw_text": raw_text,
                        "spec": spec,
                        "raw_price": raw_price,
                        "price": str(price),
                        "locator": locator,
                        "resolved_by": str(actor_id),
                        "note": note,
                    }
                ),
                tenant_id,
                group_id,
            ),
        )

    return get_group(conn, tenant_id, group_id)


def _candidate_count(
    conn: psycopg.Connection[Any], tenant_id: UUID, group_id: UUID
) -> int:
    row = conn.execute(
        "SELECT count(*) FROM catalog_conflict_candidates"
        " WHERE tenant_id = %s AND group_id = %s",
        (tenant_id, group_id),
    ).fetchone()
    return int(row[0]) if row is not None else 0


def get_group(conn: psycopg.Connection[Any], tenant_id: UUID, group_id: UUID) -> ConflictGroup:
    bind_tenant(conn, tenant_id)
    row = conn.execute(
        """SELECT group_id, version_id, code, status, selected_candidate_id, resolved_by, note
             FROM catalog_conflict_groups WHERE tenant_id = %s AND group_id = %s""",
        (tenant_id, group_id),
    ).fetchone()
    if row is None:
        raise ConflictNotFound(str(group_id))
    return ConflictGroup(
        group_id=UUID(str(row[0])),
        version_id=UUID(str(row[1])),
        code=str(row[2]),
        status=str(row[3]),
        selected_candidate_id=UUID(str(row[4])) if row[4] else None,
        resolved_by=UUID(str(row[5])) if row[5] else None,
        note=row[6],
        candidates=group_candidates(conn, tenant_id, UUID(str(row[0]))),
    )


def open_summary(conn: psycopg.Connection[Any], tenant_id: UUID) -> tuple[int, int]:
    """返回 (待办组数, 候选行数)——统计单位刻意分开，避免把候选当货号数。"""
    bind_tenant(conn, tenant_id)
    row = conn.execute(
        """SELECT count(*), coalesce(sum(c), 0) FROM (
             SELECT g.group_id, (SELECT count(*) FROM catalog_conflict_candidates c
                                  WHERE c.tenant_id = g.tenant_id AND c.group_id = g.group_id) AS c
               FROM catalog_conflict_groups g
              WHERE g.tenant_id = %s AND g.status = 'OPEN'
           ) t""",
        (tenant_id,),
    ).fetchone()
    return (int(row[0]), int(row[1])) if row else (0, 0)


def list_open_groups(
    conn: psycopg.Connection[Any], tenant_id: UUID, *, limit: int = 50, offset: int = 0
) -> tuple[ConflictGroup, ...]:
    rows = conn.execute(
        """SELECT group_id FROM catalog_conflict_groups
            WHERE tenant_id = %s AND status = 'OPEN'
            ORDER BY created_at, code
            LIMIT %s OFFSET %s""",
        (tenant_id, limit, offset),
    ).fetchall()
    return tuple(get_group(conn, tenant_id, UUID(str(row[0]))) for row in rows)


__all__ = [
    "ConflictCandidate",
    "ConflictError",
    "ConflictGroup",
    "ConflictNotFound",
    "OPERABLE_STATUSES",
    "add_candidates",
    "create_group",
    "get_group",
    "group_candidates",
    "list_open_groups",
    "lock_group",
    "bind_tenant",
    "open_summary",
    "require_operator",
    "resolve_group",
]
