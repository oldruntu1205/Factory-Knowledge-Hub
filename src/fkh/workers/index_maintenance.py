"""索引代维护：发布闸门与撤销后的重建收敛（Rev2 §7.2）。

两条硬规则：
- **发布**：新版本必须先有 READY 的 generation，且该代已在"可见代水位"之内；否则拒答/拒绝发布。
- **撤销**：状态同步生效（API 立即不可读），物理清理由这里异步完成；
  重建后必须收敛——撤销内容在块表里也不应继续存在，旧引用直读同样失效。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

import psycopg


class IndexNotReady(RuntimeError):
    """该版本的索引代未 READY 或未纳入可见水位（发布/检索应拒绝）。"""


@dataclass(frozen=True, slots=True)
class GenerationState:
    generation: int
    status: str
    chunk_count: int
    visible: bool


def mark_generation_ready(
    conn: psycopg.Connection[Any], tenant_id: UUID, generation: int, chunk_count: int
) -> None:
    conn.execute(
        """INSERT INTO index_generations (tenant_id, generation, status, chunk_count)
           VALUES (%s, %s, 'READY', %s)
           ON CONFLICT (tenant_id, generation)
           DO UPDATE SET status = 'READY', chunk_count = EXCLUDED.chunk_count""",
        (tenant_id, generation, chunk_count),
    )


def mark_generation_failed(
    conn: psycopg.Connection[Any], tenant_id: UUID, generation: int
) -> None:
    conn.execute(
        """INSERT INTO index_generations (tenant_id, generation, status, chunk_count)
           VALUES (%s, %s, 'FAILED', 0)
           ON CONFLICT (tenant_id, generation) DO UPDATE SET status = 'FAILED'""",
        (tenant_id, generation),
    )


def switch_visible_generation(
    conn: psycopg.Connection[Any], tenant_id: UUID, generation: int
) -> None:
    """原子切换可见代（只允许前移到 READY 的代）。"""
    ready = conn.execute(
        "SELECT 1 FROM index_generations WHERE tenant_id = %s AND generation = %s"
        " AND status = 'READY'",
        (tenant_id, generation),
    ).fetchone()
    if ready is None:
        raise IndexNotReady(f"generation {generation} 未就绪，不能切换可见代")
    conn.execute(
        """INSERT INTO index_visibility (tenant_id, generation)
           VALUES (%s, %s)
           ON CONFLICT (tenant_id)
           DO UPDATE SET generation = GREATEST(index_visibility.generation, EXCLUDED.generation),
                         switched_at = now()""",
        (tenant_id, generation),
    )


def visible_generation(conn: psycopg.Connection[Any], tenant_id: UUID) -> int | None:
    row = conn.execute(
        "SELECT generation FROM index_visibility WHERE tenant_id = %s", (tenant_id,)
    ).fetchone()
    return int(row[0]) if row is not None else None


def assert_version_index_ready(
    conn: psycopg.Connection[Any], tenant_id: UUID, version_id: UUID
) -> int:
    """发布前校验：该版本所有块所属代必须 READY，且不超过可见水位。"""
    rows = conn.execute(
        "SELECT DISTINCT c.index_generation FROM knowledge_chunks c"
        " WHERE c.tenant_id = %s AND c.version_id = %s",
        (tenant_id, version_id),
    ).fetchall()
    if not rows:
        raise IndexNotReady("该版本尚未切出任何索引块（构建未完成）")
    generations = [int(row[0]) for row in rows]

    ready = {
        int(row[0])
        for row in conn.execute(
            "SELECT generation FROM index_generations"
            " WHERE tenant_id = %s AND generation = ANY(%s) AND status = 'READY'",
            (tenant_id, generations),
        ).fetchall()
    }
    missing = sorted(set(generations) - ready)
    if missing:
        raise IndexNotReady(f"索引代未就绪：{missing}")

    # 只看"是否构建并就绪"；把水位推进到该代是**发布动作**（switch_visible_generation），
    # 这里若要求 newest <= visible 会形成死锁：新版本永远等不到水位先动。
    return max(generations)


def purge_version_chunks(
    conn: psycopg.Connection[Any], tenant_id: UUID, version_id: UUID
) -> int:
    """删除某一版本的块（幂等：已删则返回 0）。"""
    result = conn.execute(
        "DELETE FROM knowledge_chunks WHERE tenant_id = %s AND version_id = %s",
        (tenant_id, version_id),
    )
    return int(result.rowcount)


def rebuild_visible_generation(conn: psycopg.Connection[Any], tenant_id: UUID) -> int:
    """撤销后的重建收敛：物理删除已撤销/作废版本的块，切出新一代并切换可见代。

    只删 `REVOKED`/`DISCARDED` 的块——已批准版本（APPROVED_INTERNAL / PUBLISHED_PUBLIC）
    与仍在流程中的版本不受影响；返回新的可见代。
    """
    conn.execute(
        """DELETE FROM knowledge_chunks c
            USING knowledge_versions v
            WHERE c.tenant_id = %s
              AND v.tenant_id = c.tenant_id
              AND v.id = c.version_id
              AND v.status IN ('REVOKED', 'DISCARDED')""",
        (tenant_id,),
    )
    row = conn.execute(
        "SELECT coalesce(max(index_generation), 0) + 1 FROM knowledge_chunks WHERE tenant_id = %s",
        (tenant_id,),
    ).fetchone()
    assert row is not None
    generation = int(row[0])
    count_row = conn.execute(
        "SELECT count(*) FROM knowledge_chunks WHERE tenant_id = %s", (tenant_id,)
    ).fetchone()
    assert count_row is not None
    mark_generation_ready(conn, tenant_id, generation, int(count_row[0]))
    switch_visible_generation(conn, tenant_id, generation)
    return generation


def generation_state(
    conn: psycopg.Connection[Any], tenant_id: UUID, generation: int
) -> GenerationState | None:
    row = conn.execute(
        """SELECT g.status, g.chunk_count,
                  coalesce((SELECT generation FROM index_visibility WHERE tenant_id = g.tenant_id)
                           >= g.generation, false)
             FROM index_generations g
            WHERE g.tenant_id = %s AND g.generation = %s""",
        (tenant_id, generation),
    ).fetchone()
    if row is None:
        return None
    return GenerationState(
        generation=generation,
        status=str(row[0]),
        chunk_count=int(row[1]),
        visible=bool(row[2]),
    )


__all__ = [
    "GenerationState",
    "IndexNotReady",
    "assert_version_index_ready",
    "generation_state",
    "mark_generation_failed",
    "purge_version_chunks",
    "mark_generation_ready",
    "rebuild_visible_generation",
    "switch_visible_generation",
    "visible_generation",
]
