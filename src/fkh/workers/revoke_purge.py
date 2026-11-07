"""撤销清理的持久消费闭环（L1 2026-11-08 阻断项 3）。

职责：消费 `knowledge.version.revoked.v1` / `...discarded.v1`，物理删除对应版本的块。

闭环要求（逐条对应 L1 要求）：
- **持久**：事件在 `outbox_events`，消费状态在 `outbox_deliveries`，进程崩了下次接着做；
- **幂等**：`(tenant, event_id, consumer)` 为主键；已 DONE 的事件不再重复处理；
- **重试**：失败累加 `attempts`，下次本轮再试；
- **死信/可追踪失败**：达到上限转 `FAILED` 并记录错误，可用 `reset_failed` 补偿重入。

通用消费框架留给 ADR-06；这里只承担撤销清理这一条责任。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import psycopg

CONSUMER = "revoke-purge"
REVOKE_EVENTS = ("knowledge.version.revoked.v1", "knowledge.version.discarded.v1")
MAX_ATTEMPTS = 5


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    processed: int
    failed: int
    dead_lettered: int


def _pending_events(conn: psycopg.Connection[Any], tenant_id: UUID) -> list[tuple[UUID, str, UUID]]:
    rows = conn.execute(
        """SELECT e.id, e.event_type, e.aggregate_id
             FROM outbox_events e
             LEFT JOIN outbox_deliveries d
               ON d.tenant_id = e.tenant_id AND d.event_id = e.id AND d.consumer = %(consumer)s
            WHERE e.tenant_id = %(tenant)s
              AND e.event_type = ANY(%(types)s)
              AND (d.status IS NULL OR (d.status = 'PENDING' AND d.attempts < %(max_attempts)s))
            ORDER BY e.occurred_at""",
        {
            "tenant": tenant_id,
            "consumer": CONSUMER,
            "types": list(REVOKE_EVENTS),
            "max_attempts": MAX_ATTEMPTS,
        },
    ).fetchall()
    return [(UUID(str(row[0])), str(row[1]), UUID(str(row[2]))) for row in rows]


def _mark(
    conn: psycopg.Connection[Any],
    tenant_id: UUID,
    event_id: UUID,
    *,
    status: str,
    error: str | None = None,
) -> None:
    conn.execute(
        """INSERT INTO outbox_deliveries
             (tenant_id, event_id, consumer, status, attempts, last_error)
           VALUES (%s, %s, %s, %s, 1, %s)
           ON CONFLICT (tenant_id, event_id, consumer)
           DO UPDATE SET status = EXCLUDED.status,
                         attempts = outbox_deliveries.attempts + 1,
                         last_error = EXCLUDED.last_error,
                         updated_at = now()""",
        (tenant_id, event_id, CONSUMER, status, error),
    )


def consume_revoke_events(
    conn: psycopg.Connection[Any],
    tenant_id: UUID,
    *,
    purge: Callable[[psycopg.Connection[Any], UUID, UUID], int] | None = None,
    limit: int = 50,
) -> DeliveryResult:
    """处理一批撤销事件。`purge` 可注入（便于故障注入测试）。"""
    from fkh.workers.index_maintenance import purge_version_chunks

    do_purge = purge or purge_version_chunks
    events: Iterable[tuple[UUID, str, UUID]] = _pending_events(conn, tenant_id)[:limit]

    processed = failed = dead = 0
    for event_id, _event_type, version_id in events:
        try:
            removed = do_purge(conn, tenant_id, version_id)
            _mark(conn, tenant_id, event_id, status="DONE")
            conn.commit()
            processed += 1
            del removed
        except Exception as exc:  # noqa: BLE001 - 消费失败要留痕而不是中断整批
            conn.rollback()
            attempts_row = conn.execute(
                "SELECT attempts FROM outbox_deliveries WHERE tenant_id = %s AND event_id = %s"
                " AND consumer = %s",
                (tenant_id, event_id, CONSUMER),
            ).fetchone()
            attempts = int(attempts_row[0]) if attempts_row else 0
            exhausted = attempts + 1 >= MAX_ATTEMPTS
            _mark(
                conn,
                tenant_id,
                event_id,
                status="FAILED" if exhausted else "PENDING",
                error=str(exc)[:500],
            )
            conn.commit()
            failed += 1
            dead += 1 if exhausted else 0
    return DeliveryResult(processed=processed, failed=failed, dead_lettered=dead)


def reset_failed(conn: psycopg.Connection[Any], tenant_id: UUID) -> int:
    """补偿入口：把死信重置为 PENDING（attempts 归零），允许重新消费。"""
    result = conn.execute(
        """UPDATE outbox_deliveries
              SET status = 'PENDING', attempts = 0, updated_at = now()
            WHERE tenant_id = %s AND consumer = %s AND status = 'FAILED'""",
        (tenant_id, CONSUMER),
    )
    conn.commit()
    return result.rowcount


def failed_deliveries(conn: psycopg.Connection[Any], tenant_id: UUID) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT event_id, attempts, last_error, updated_at
             FROM outbox_deliveries
            WHERE tenant_id = %s AND consumer = %s AND status = 'FAILED'
            ORDER BY updated_at DESC""",
        (tenant_id, CONSUMER),
    ).fetchall()
    return [
        {
            "event_id": str(row[0]),
            "attempts": int(row[1]),
            "last_error": row[2],
            "updated_at": row[3].isoformat() if row[3] else None,
        }
        for row in rows
    ]


__all__ = [
    "CONSUMER",
    "DeliveryResult",
    "consume_revoke_events",
    "failed_deliveries",
    "reset_failed",
]
