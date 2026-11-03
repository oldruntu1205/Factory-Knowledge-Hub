"""outbox 事件投递器：至少一次投递、租约令牌、指数退避与死信。

为什么不是一个「扫表 + HTTP」的循环：
- 投递是网络动作，数据库行锁不能跨网络持有。领取必须先提交事务、生成租约与令牌，
  再用令牌确认；否则 worker 崩溃会把行锁拖到连接超时，期间该事件无法被任何人接手。
- `retry_count` 在**领取时**加一，不在失败时加。这样「领完就崩」也会消耗预算，
  第 N 次领取后崩溃的事件会在租约过期后被结算为死信，而不是永久悬挂。
- 顺序按 `(aggregate_version, event_ordinal)`，不按 `occurred_at`：同一事务内
  `now()` 恒定、UUID 随机，两者都不能表达业务顺序。前驱包含退避中与死信的，
  死信因此会阻塞同聚合的后续事件——这是有意的，宁可停住也不能乱序放开索引。
- 传输层未配置时拒绝启动（fail-closed）。绝不把事件标成已发布来「让队列看起来是空的」。

权限模型（L1 判决 2026-11-03）：
relay 使用平台角色 `fkh_relay`（BYPASSRLS，仅 SELECT + 投递状态列的列级 UPDATE）。
业务角色 `fkh_app` 在未设置 `app.tenant_id` 时对 outbox 零行可见。
角色与授权脚本见 `deploy/sql/relay_role.sql`，迁移只负责 schema。
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import UUID

import psycopg

from fkh.config import Settings

logger = logging.getLogger(__name__)

ENVELOPE_SCHEMA_VERSION = "1.0"
MAX_ERROR_CHARS = 500
WEBHOOK_SUCCESS_STATUS = frozenset({200, 201, 202, 204})


class TransportError(RuntimeError):
    """投递失败。按可重试处理，由 relay 记退避与死信。"""


class TransportNotConfigured(RuntimeError):
    """没有可用传输层。relay 必须拒绝启动，而不是静默把事件标成已发布。"""


class OutboxReplayRejected(ValueError):
    """重放目标不是死信事件（例如已发布），拒绝执行。"""


class Transport(Protocol):
    """传输契约。

    实现方只在**投递成功**时正常返回；任何不确定都抛 `TransportError`。
    消费者侧必须按 `(tenant_id, event_id)` 幂等去重：租约只能阻止旧 worker 改数据库，
    无法撤回已经发出的请求。
    """

    def deliver(self, envelope: dict[str, Any]) -> None: ...


@dataclass(frozen=True, slots=True)
class RelayConfig:
    batch_size: int = 20
    lease_seconds: int = 30
    max_attempts: int = 8
    backoff_base_seconds: float = 2.0
    backoff_cap_seconds: float = 300.0
    producer: str = "knowledge-api"

    def __post_init__(self) -> None:
        if self.batch_size <= 0:
            raise ValueError("batch_size 必须为正")
        if self.lease_seconds <= 0:
            raise ValueError("lease_seconds 必须为正")
        if self.max_attempts < 1:
            raise ValueError("max_attempts 至少为 1")
        if self.backoff_base_seconds <= 0:
            raise ValueError("backoff_base_seconds 必须为正")
        if self.backoff_cap_seconds < self.backoff_base_seconds:
            raise ValueError("backoff_cap_seconds 不得小于 backoff_base_seconds")
        if not self.producer.strip():
            raise ValueError("producer 不得为空：封套要求可追溯的生产者名")

    def backoff_seconds(self, attempt: int) -> float:
        """第 attempt 次领取失败后的等待时长：min(base × 2^(attempt−1), cap)。

        指数用浮点累乘而非 `2**attempt`：attempt 可能被人工改大，
        整数幂会先溢出再被 min() 截断，得到一个看似正常的错值。
        """
        if attempt < 1:
            raise ValueError("attempt 从 1 开始计数")
        delay = self.backoff_base_seconds
        for _ in range(attempt - 1):
            if delay >= self.backoff_cap_seconds:
                return self.backoff_cap_seconds
            delay *= 2
        return min(delay, self.backoff_cap_seconds)


@dataclass(frozen=True, slots=True)
class ClaimedEvent:
    id: UUID
    tenant_id: UUID
    aggregate_type: str
    aggregate_id: UUID
    aggregate_version: int
    event_ordinal: int
    event_type: str
    payload: dict[str, Any]
    occurred_at: datetime
    retry_count: int
    delivery_token: UUID
    trace_id: str | None

    def envelope(self, producer: str) -> dict[str, Any]:
        """按 L1 §7.1 组装封套。

        `trace_id` 为空时用事件 id 填充，并在文档中明确：此时它只表达「事件自身链路」，
        不代表上游请求 trace。重试与重放必须复用同一份封套内容，不得临时生成新 trace。
        """
        return {
            "schema_version": ENVELOPE_SCHEMA_VERSION,
            "event_id": str(self.id),
            "event_type": self.event_type,
            "tenant_id": str(self.tenant_id),
            "aggregate_id": str(self.aggregate_id),
            "aggregate_version": self.aggregate_version,
            "occurred_at": self.occurred_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
            "trace_id": self.trace_id or str(self.id),
            "producer": producer,
            "payload": self.payload,
        }


@dataclass(frozen=True, slots=True)
class RelayRunResult:
    claimed: int = 0
    delivered: int = 0
    failed: int = 0
    dead_lettered: int = 0
    lease_lost: int = 0
    expired_recovered: int = 0


_CLAIM_SQL = """
WITH due AS (
  SELECT o.id
    FROM outbox_events o
   WHERE o.published_at IS NULL
     AND o.dead_lettered_at IS NULL
     AND o.retry_count < %(max_attempts)s
     AND o.next_attempt_at <= clock_timestamp()
     AND (o.locked_until IS NULL OR o.locked_until < clock_timestamp())
     AND NOT EXISTS (
       SELECT 1
         FROM outbox_events p
        WHERE p.published_at IS NULL
          AND p.tenant_id = o.tenant_id
          AND p.aggregate_type = o.aggregate_type
          AND p.aggregate_id = o.aggregate_id
          AND (p.aggregate_version, p.event_ordinal)
              < (o.aggregate_version, o.event_ordinal)
     )
   ORDER BY o.next_attempt_at, o.occurred_at, o.id
   LIMIT %(batch_size)s
     FOR UPDATE SKIP LOCKED
)
UPDATE outbox_events t
   SET locked_until   = clock_timestamp() + make_interval(secs => %(lease_seconds)s),
       delivery_token = gen_random_uuid(),
       retry_count    = t.retry_count + 1
  FROM due
 WHERE t.id = due.id
RETURNING t.id, t.tenant_id, t.aggregate_type, t.aggregate_id, t.aggregate_version,
          t.event_ordinal, t.event_type, t.payload, t.occurred_at, t.retry_count,
          t.delivery_token, t.trace_id
"""

_LEASE_GUARD = """
      id = %(event_id)s
  AND delivery_token = %(token)s
  AND locked_until > clock_timestamp()
  AND published_at IS NULL
  AND dead_lettered_at IS NULL
"""

_ACK_SQL = f"""
UPDATE outbox_events
   SET published_at   = clock_timestamp(),
       locked_until   = NULL,
       delivery_token = NULL,
       last_error     = NULL
 WHERE {_LEASE_GUARD}
"""

_FAIL_SQL = f"""
UPDATE outbox_events
   SET locked_until   = NULL,
       delivery_token = NULL,
       last_error     = %(error)s,
       next_attempt_at = clock_timestamp() + make_interval(secs => %(backoff)s)
 WHERE {_LEASE_GUARD}
"""

_DEAD_LETTER_SQL = f"""
UPDATE outbox_events
   SET dead_lettered_at = clock_timestamp(),
       locked_until     = NULL,
       delivery_token   = NULL,
       last_error       = %(error)s
 WHERE {_LEASE_GUARD}
"""

_RECOVER_EXPIRED_SQL = """
UPDATE outbox_events
   SET dead_lettered_at = clock_timestamp(),
       locked_until     = NULL,
       delivery_token   = NULL,
       last_error       = coalesce(last_error, 'lease expired at max attempts')
 WHERE published_at IS NULL
   AND dead_lettered_at IS NULL
   AND locked_until IS NOT NULL
   AND locked_until < clock_timestamp()
   AND retry_count >= %(max_attempts)s
"""


class OutboxRelay:
    """投递器。连接必须是以平台角色（BYPASSRLS）建立的，否则跨租户扫描只见零行。"""

    def __init__(
        self,
        conn: psycopg.Connection[Any],
        transport: Transport,
        config: RelayConfig | None = None,
    ) -> None:
        self._conn = conn
        self._transport = transport
        self._config = config or RelayConfig()

    @property
    def config(self) -> RelayConfig:
        return self._config

    def claim(self, limit: int | None = None) -> list[ClaimedEvent]:
        """领取一批事件并**立即提交**，租约从此开始计时。

        提交是租约能被其他 worker 观察到的前提；留在未提交事务里等于行锁，
        投递一旦卡住就会把同聚合的后续事件一起堵死到连接超时。
        """
        batch = limit if limit is not None else self._config.batch_size
        rows = self._conn.execute(
            _CLAIM_SQL,
            {
                "max_attempts": self._config.max_attempts,
                "batch_size": batch,
                "lease_seconds": self._config.lease_seconds,
            },
        ).fetchall()
        self._conn.commit()
        return [self._to_claimed(row) for row in rows]

    def deliver_claimed(self, event: ClaimedEvent) -> str:
        """投递单条事件，返回 `delivered` / `failed` / `dead_lettered` / `lease_lost`。"""
        try:
            self._transport.deliver(event.envelope(self._config.producer))
        except TransportError as exc:
            return self._record_failure(event, str(exc))
        except Exception as exc:  # noqa: BLE001 - 传输层任何异常都不得让事件停在半空
            return self._record_failure(event, f"{type(exc).__name__}: {exc}")
        return self._record_success(event)

    def run_once(self, limit: int | None = None) -> RelayRunResult:
        """领取一批 → 逐条投递 → 结算过期租约。"""
        claimed = self.claim(limit)
        delivered = failed = dead = lost = 0
        for event in claimed:
            outcome = self.deliver_claimed(event)
            if outcome == "delivered":
                delivered += 1
            elif outcome == "dead_lettered":
                dead += 1
            elif outcome == "failed":
                failed += 1
            else:
                lost += 1
        recovered = self.recover_expired()
        return RelayRunResult(
            claimed=len(claimed),
            delivered=delivered,
            failed=failed,
            dead_lettered=dead,
            lease_lost=lost,
            expired_recovered=recovered,
        )

    def recover_expired(self) -> int:
        """把「领满次数且租约已过期」的事件结算为死信。

        没有这一步，第 N 次领取后崩溃的事件会永远停在「有租约、无人确认」的状态：
        领取条件要求 `retry_count < max_attempts`，它已经拿不到，也不会被标死。
        """
        cursor = self._conn.execute(_RECOVER_EXPIRED_SQL, {"max_attempts": self._config.max_attempts})
        self._conn.commit()
        return cursor.rowcount

    # ---- 结算 --------------------------------------------------------------
    def _record_success(self, event: ClaimedEvent) -> str:
        cursor = self._conn.execute(_ACK_SQL, {"event_id": event.id, "token": event.delivery_token})
        self._conn.commit()
        if cursor.rowcount == 0:
            logger.warning(
                "outbox 租约已失效，放弃确认（事件可能已被他人接手）: event_id=%s", event.id
            )
            return "lease_lost"
        return "delivered"

    def _record_failure(self, event: ClaimedEvent, error: str) -> str:
        message = _truncate(error)
        params = {"event_id": event.id, "token": event.delivery_token, "error": message}
        if event.retry_count >= self._config.max_attempts:
            cursor = self._conn.execute(_DEAD_LETTER_SQL, params)
            self._conn.commit()
            if cursor.rowcount == 0:
                return "lease_lost"
            logger.error("outbox 事件进入死信: event_id=%s attempts=%s", event.id, event.retry_count)
            return "dead_lettered"

        params["backoff"] = self._config.backoff_seconds(event.retry_count)
        cursor = self._conn.execute(_FAIL_SQL, params)
        self._conn.commit()
        if cursor.rowcount == 0:
            return "lease_lost"
        return "failed"

    @staticmethod
    def _to_claimed(row: tuple[Any, ...]) -> ClaimedEvent:
        return ClaimedEvent(
            id=UUID(str(row[0])),
            tenant_id=UUID(str(row[1])),
            aggregate_type=str(row[2]),
            aggregate_id=UUID(str(row[3])),
            aggregate_version=int(row[4]),
            event_ordinal=int(row[5]),
            event_type=str(row[6]),
            payload=dict(row[7]),
            occurred_at=row[8],
            retry_count=int(row[9]),
            delivery_token=UUID(str(row[10])),
            trace_id=str(row[11]) if row[11] is not None else None,
        )


# ---------------------------------------------------------------------------
# 运维视角：死信可见性与人工重放
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class DeadLetter:
    tenant_id: UUID
    event_id: UUID
    aggregate_type: str
    aggregate_id: UUID
    aggregate_version: int
    event_ordinal: int
    event_type: str
    retry_count: int
    last_error: str | None
    dead_lettered_at: datetime
    blocks_successors: bool


_DEAD_LETTERS_SQL = """
SELECT o.tenant_id, o.id, o.aggregate_type, o.aggregate_id, o.aggregate_version,
       o.event_ordinal, o.event_type, o.retry_count, o.last_error, o.dead_lettered_at,
       EXISTS (
         SELECT 1
           FROM outbox_events s
          WHERE s.published_at IS NULL
            AND s.dead_lettered_at IS NULL
            AND s.tenant_id = o.tenant_id
            AND s.aggregate_type = o.aggregate_type
            AND s.aggregate_id = o.aggregate_id
            AND (s.aggregate_version, s.event_ordinal)
                > (o.aggregate_version, o.event_ordinal)
       ) AS blocks_successors
  FROM outbox_events o
 WHERE o.dead_lettered_at IS NOT NULL
 ORDER BY o.dead_lettered_at DESC, o.id
 LIMIT %(limit)s
"""


def dead_letters(conn: psycopg.Connection[Any], limit: int = 100) -> list[DeadLetter]:
    """列出死信，并标出它是否正在阻塞同聚合的后续事件。

    被阻塞的聚合不会自动恢复：要么人工重放，要么显式废弃，不能靠「等它自己好」。
    """
    rows = conn.execute(_DEAD_LETTERS_SQL, {"limit": limit}).fetchall()
    conn.rollback()
    return [
        DeadLetter(
            tenant_id=UUID(str(row[0])),
            event_id=UUID(str(row[1])),
            aggregate_type=str(row[2]),
            aggregate_id=UUID(str(row[3])),
            aggregate_version=int(row[4]),
            event_ordinal=int(row[5]),
            event_type=str(row[6]),
            retry_count=int(row[7]),
            last_error=str(row[8]) if row[8] is not None else None,
            dead_lettered_at=row[9],
            blocks_successors=bool(row[10]),
        )
        for row in rows
    ]


def dead_letter_count(conn: psycopg.Connection[Any]) -> int:
    row = conn.execute(
        "SELECT count(*) FROM outbox_events WHERE dead_lettered_at IS NOT NULL"
    ).fetchone()
    conn.rollback()
    return int(row[0]) if row is not None else 0


_REPLAY_SQL = """
UPDATE outbox_events
   SET dead_lettered_at = NULL,
       retry_count      = 0,
       locked_until     = NULL,
       delivery_token   = NULL,
       last_error       = NULL,
       next_attempt_at  = clock_timestamp()
 WHERE tenant_id = %(tenant_id)s
   AND id = %(event_id)s
   AND dead_lettered_at IS NOT NULL
   AND published_at IS NULL
"""


def replay_dead_letter(
    conn: psycopg.Connection[Any],
    *,
    tenant_id: UUID,
    event_id: UUID,
    actor_id: UUID,
    reason: str,
) -> None:
    """人工重放死信：清空死信/次数/租约，令其立即可领，并留下审计。

    只对死信开放。已发布事件不得由此重投——重投已发布事件会改变历史消费顺序，
    必须走单独的显式修复流程（L1 判决第 7 条）。
    """
    if not reason.strip():
        raise ValueError("重放必须写明原因，否则审计无法回答『为什么又发一次』")

    cursor = conn.execute(_REPLAY_SQL, {"tenant_id": tenant_id, "event_id": event_id})
    if cursor.rowcount == 0:
        conn.rollback()
        raise OutboxReplayRejected(
            "目标不是死信事件（可能已发布、已重放或不存在），拒绝重放"
        )
    conn.execute(
        """INSERT INTO outbox_replays (tenant_id, event_id, actor_id, reason)
           VALUES (%s, %s, %s, %s)""",
        (tenant_id, event_id, actor_id, reason.strip()),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# 传输实现
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class WebhookTransport:
    """把封套 POST 给接收方。

    成功码是白名单而不是「2xx 就算过」：若对端把事件存进一个 3xx/404 的兜底路由并返回成功，
    `published_at` 会被置位，事件从此消失于重试视野。宁可判失败退避，也不要假送达。
    """

    url: str
    timeout_seconds: float = 10.0

    def deliver(self, envelope: dict[str, Any]) -> None:
        body = json.dumps(envelope, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(  # noqa: S310 - URL 来自部署配置，不接受请求输入
            self.url,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Idempotency-Key": str(envelope["event_id"]),
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:  # noqa: S310
                status = int(response.status)
        except urllib.error.HTTPError as exc:
            raise TransportError(f"webhook 返回 {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise TransportError(f"webhook 不可达: {exc}") from exc

        if status not in WEBHOOK_SUCCESS_STATUS:
            raise TransportError(f"webhook 返回非成功状态 {status}")


def build_transport(settings: Settings) -> Transport:
    """按配置构造传输层。未配置即抛错——relay 不得在没有出口时假装投递成功。"""
    kind = (settings.outbox_transport or "").strip().lower()
    if kind == "webhook":
        if not settings.outbox_webhook_url:
            raise TransportNotConfigured(
                "FKH_OUTBOX_TRANSPORT=webhook 但未配置 FKH_OUTBOX_WEBHOOK_URL"
            )
        return WebhookTransport(
            url=settings.outbox_webhook_url,
            timeout_seconds=settings.outbox_timeout_seconds,
        )
    raise TransportNotConfigured(
        f"未配置可用的 outbox 传输层（当前 FKH_OUTBOX_TRANSPORT={settings.outbox_transport!r}）；"
        "relay 拒绝启动，事件保持在待投递状态"
    )


def relay_config_from_settings(settings: Settings) -> RelayConfig:
    return RelayConfig(
        batch_size=settings.outbox_batch_size,
        lease_seconds=settings.outbox_lease_seconds,
        max_attempts=settings.outbox_max_attempts,
        backoff_base_seconds=settings.outbox_backoff_base_seconds,
        backoff_cap_seconds=settings.outbox_backoff_cap_seconds,
        producer=settings.outbox_producer,
    )


def _truncate(message: str) -> str:
    """`last_error` 有 CHECK 长度上限：先截断再写，否则错误信息本身会变成写入失败。"""
    if len(message) <= MAX_ERROR_CHARS:
        return message
    return message[: MAX_ERROR_CHARS - 12] + "…[已截断]"


__all__ = [
    "ClaimedEvent",
    "DeadLetter",
    "OutboxRelay",
    "OutboxReplayRejected",
    "RelayConfig",
    "RelayRunResult",
    "Transport",
    "TransportError",
    "TransportNotConfigured",
    "WebhookTransport",
    "build_transport",
    "dead_letter_count",
    "dead_letters",
    "relay_config_from_settings",
    "replay_dead_letter",
]
