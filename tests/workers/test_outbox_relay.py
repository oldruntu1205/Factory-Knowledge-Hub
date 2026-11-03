"""outbox 投递器门禁：租约、顺序、重试预算、死信、重放与权限边界。

逐条对应 2026-11-03 L1 判决「必须补的测试」1–8 条（_evidence/l1_reply_outbox_20261103.md）。
"""

from __future__ import annotations

import json
import urllib.error
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from jsonschema import Draft202012Validator, FormatChecker

from fkh.config import Settings
from fkh.workers.outbox_relay import (
    OutboxRelay,
    OutboxReplayRejected,
    RelayConfig,
    TransportError,
    TransportNotConfigured,
    WebhookTransport,
    build_transport,
    dead_letter_count,
    dead_letters,
    replay_dead_letter,
)

pytestmark = pytest.mark.requires_pg

ROOT = Path(__file__).resolve().parents[2]
SCHEMA_DIR = ROOT / "contracts" / "schema"
EVENT_TYPE = "knowledge.version.published.v1"
AGGREGATE_TYPE = "knowledge_version"


# ---------------------------------------------------------------------------
# 装置
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def clean_outbox(pg_url: str) -> Iterator[None]:
    """清空 outbox，保证每个用例只看到自己造的事件。

    测试库是**跨会话持久**的（pgdata 复用），历史事件仍在待投递状态；
    relay 是全局扫描，不隔离就会先领到上一轮遗留的事件，断言随之错位。
    """
    with psycopg.connect(pg_url, autocommit=True) as conn:
        # 用 TRUNCATE 而不是 DELETE：outbox_replays 的追加式触发器会拦下 DELETE，
        # 而 TRUNCATE 只触发行级触发器，正好用于测试库之间的清场。
        conn.execute("TRUNCATE outbox_replays, outbox_events CASCADE")
    yield


@dataclass
class RecordingTransport:
    """假传输层：记录封套、可切换失败。

    `delivered` 记录收到的请求次数；`applied` 模拟消费者侧按 (tenant_id,event_id)
    幂等去重后的业务副作用集合——重复投递会进来两次，但只会被应用一次。
    """

    fail: bool = False
    attempts: int = 0
    delivered: list[dict[str, Any]] = field(default_factory=list)
    applied: set[str] = field(default_factory=set)

    def deliver(self, envelope: dict[str, Any]) -> None:
        self.attempts += 1
        if self.fail:
            raise TransportError("receiver unreachable")
        self.delivered.append(envelope)
        self.applied.add(f"{envelope['tenant_id']}:{envelope['event_id']}")


def _insert_event(
    admin: psycopg.Connection[Any],
    tenant_id: str,
    *,
    aggregate_id: UUID | None = None,
    version: int = 1,
    ordinal: int = 0,
    event_type: str = EVENT_TYPE,
    payload: dict[str, Any] | None = None,
    trace_id: str | None = None,
    occurred_at: datetime | None = None,
    retry_count: int = 0,
) -> UUID:
    row = admin.execute(
        """INSERT INTO outbox_events
             (tenant_id, aggregate_type, aggregate_id, aggregate_version, event_type,
              payload, trace_id, occurred_at, event_ordinal, retry_count)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
           RETURNING id""",
        (
            tenant_id,
            AGGREGATE_TYPE,
            aggregate_id or uuid4(),
            version,
            event_type,
            psycopg.types.json.Jsonb(payload or {"version_id": str(uuid4())}),
            trace_id,
            occurred_at or datetime.now(UTC),
            ordinal,
            retry_count,
        ),
    ).fetchone()
    assert row is not None
    return UUID(str(row[0]))


def _make_due(admin: psycopg.Connection[Any], event_id: UUID) -> None:
    """把退避时间拨回现在，避免测试真的等 2 秒。"""
    admin.execute("UPDATE outbox_events SET next_attempt_at = now() WHERE id = %s", (event_id,))
    admin.commit()


def _expire_lease(admin: psycopg.Connection[Any], event_id: UUID) -> None:
    admin.execute(
        "UPDATE outbox_events SET locked_until = now() - interval '1 second' WHERE id = %s",
        (event_id,),
    )
    admin.commit()


def _row(admin: psycopg.Connection[Any], event_id: UUID) -> dict[str, Any]:
    admin.rollback()
    row = admin.execute(
        """SELECT published_at, dead_lettered_at, retry_count, locked_until, delivery_token,
                  last_error, trace_id, next_attempt_at
             FROM outbox_events WHERE id = %s""",
        (event_id,),
    ).fetchone()
    assert row is not None
    admin.rollback()
    return {
        "published_at": row[0],
        "dead_lettered_at": row[1],
        "retry_count": int(row[2]),
        "locked_until": row[3],
        "delivery_token": row[4],
        "last_error": row[5],
        "trace_id": row[6],
        "next_attempt_at": row[7],
    }


def _relay(conn: psycopg.Connection[Any], transport: RecordingTransport, **kwargs: Any) -> OutboxRelay:
    return OutboxRelay(conn, transport, RelayConfig(**kwargs))


# ---------------------------------------------------------------------------
# 1. 权限隔离
# ---------------------------------------------------------------------------
def test_business_role_sees_no_outbox_rows_without_tenant(
    admin: psycopg.Connection[Any], app_conn: psycopg.Connection[Any], seed: dict[str, str]
) -> None:
    _insert_event(admin, seed["tenant_a"])
    admin.commit()

    count = app_conn.execute("SELECT count(*) FROM outbox_events").fetchone()
    assert count is not None and int(count[0]) == 0


def test_relay_role_reads_events_across_tenants(
    admin: psycopg.Connection[Any], relay_conn: psycopg.Connection[Any], seed: dict[str, str]
) -> None:
    _insert_event(admin, seed["tenant_a"])
    _insert_event(admin, seed["tenant_b"])
    admin.commit()

    claimed = _relay(relay_conn, RecordingTransport()).claim(10)
    assert {str(event.tenant_id) for event in claimed} == {seed["tenant_a"], seed["tenant_b"]}


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE outbox_events SET payload = '{}'::jsonb",
        "UPDATE outbox_events SET tenant_id = gen_random_uuid()",
        "UPDATE outbox_events SET event_type = 'knowledge.version.revoked.v1'",
    ],
)
def test_relay_role_cannot_rewrite_event_identity(
    admin: psycopg.Connection[Any], relay_conn: psycopg.Connection[Any], seed: dict[str, str], statement: str
) -> None:
    _insert_event(admin, seed["tenant_a"])
    admin.commit()

    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        relay_conn.execute(statement)
    relay_conn.rollback()


# ---------------------------------------------------------------------------
# 2. 并发领取
# ---------------------------------------------------------------------------
def test_same_event_is_leased_to_only_one_worker(
    admin: psycopg.Connection[Any], relay_conn: psycopg.Connection[Any], pg_url: str, seed: dict[str, str]
) -> None:
    _insert_event(admin, seed["tenant_a"])
    admin.commit()

    with psycopg.connect(pg_url, autocommit=False) as other:
        other.execute("SET ROLE fkh_relay")
        first = _relay(relay_conn, RecordingTransport()).claim(1)
        second = _relay(other, RecordingTransport()).claim(1)
        other.rollback()

    assert len(first) == 1
    assert second == []


def test_same_aggregate_only_earliest_ordinal_is_claimable(
    admin: psycopg.Connection[Any], relay_conn: psycopg.Connection[Any], seed: dict[str, str]
) -> None:
    aggregate_id = uuid4()
    earlier = _insert_event(admin, seed["tenant_a"], aggregate_id=aggregate_id, ordinal=0)
    later = _insert_event(
        admin,
        seed["tenant_a"],
        aggregate_id=aggregate_id,
        ordinal=1,
        event_type="knowledge.version.revoked.v1",
    )
    independent = _insert_event(admin, seed["tenant_a"], aggregate_id=uuid4())
    admin.commit()

    claimed = {event.id for event in _relay(relay_conn, RecordingTransport()).claim(10)}
    assert claimed == {earlier, independent}
    assert later not in claimed


# ---------------------------------------------------------------------------
# 3. 过期租约与令牌
# ---------------------------------------------------------------------------
def test_expired_lease_is_reclaimed_and_stale_worker_cannot_ack(
    admin: psycopg.Connection[Any], relay_conn: psycopg.Connection[Any], pg_url: str, seed: dict[str, str]
) -> None:
    event_id = _insert_event(admin, seed["tenant_a"])
    admin.commit()

    stale_transport = RecordingTransport()
    stale_relay = _relay(relay_conn, stale_transport)
    stale_event = stale_relay.claim(1)[0]

    _expire_lease(admin, event_id)

    with psycopg.connect(pg_url, autocommit=False) as other:
        other.execute("SET ROLE fkh_relay")
        fresh = _relay(other, RecordingTransport(), lease_seconds=60)
        fresh_event = fresh.claim(1)[0]
        assert fresh_event.id == event_id

        # 旧 worker 的确认必须更新零行，且不能覆盖新 worker 的租约
        assert stale_relay.deliver_claimed(stale_event) == "lease_lost"
        assert _row(admin, event_id)["published_at"] is None
        assert _row(admin, event_id)["delivery_token"] is not None
        other.rollback()


# ---------------------------------------------------------------------------
# 4. 业务顺序
# ---------------------------------------------------------------------------
def test_order_follows_version_not_uuid_or_occurred_at(
    admin: psycopg.Connection[Any], relay_conn: psycopg.Connection[Any], seed: dict[str, str]
) -> None:
    same_moment = datetime.now(UTC)
    aggregate_id = uuid4()
    # 低版本但 UUID 更大：按 UUID 排会错序
    _insert_event(
        admin,
        seed["tenant_a"],
        aggregate_id=aggregate_id,
        version=2,
        ordinal=0,
        occurred_at=same_moment + timedelta(milliseconds=1),
    )
    low_version = _insert_event(
        admin, seed["tenant_a"], aggregate_id=aggregate_id, version=1, ordinal=0, occurred_at=same_moment
    )
    admin.commit()

    claimed = _relay(relay_conn, RecordingTransport()).claim(10)
    assert [event.id for event in claimed] == [low_version]


def test_successor_blocked_by_backoff_lease_and_dead_letter(
    admin: psycopg.Connection[Any], relay_conn: psycopg.Connection[Any], seed: dict[str, str]
) -> None:
    aggregate_id = uuid4()
    first = _insert_event(admin, seed["tenant_a"], aggregate_id=aggregate_id, ordinal=0)
    successor = _insert_event(
        admin,
        seed["tenant_a"],
        aggregate_id=aggregate_id,
        ordinal=1,
        event_type="knowledge.version.revoked.v1",
    )
    other_aggregate = _insert_event(admin, seed["tenant_a"], aggregate_id=uuid4())
    admin.commit()

    failing = _relay(relay_conn, RecordingTransport(fail=True))
    event = failing.claim(1)[0]
    assert event.id == first
    assert failing.deliver_claimed(event) == "failed"

    # 退避中：后继不可领，其他聚合照常推进
    claimed = {event.id for event in _relay(relay_conn, RecordingTransport()).claim(10)}
    assert claimed == {other_aggregate}

    # 死信同样阻塞后继
    admin.execute("UPDATE outbox_events SET dead_lettered_at = now() WHERE id = %s", (first,))
    admin.commit()
    claimed = {event.id for event in _relay(relay_conn, RecordingTransport()).claim(10)}
    assert successor not in claimed


def test_duplicate_ordinal_is_rejected_by_database(
    admin: psycopg.Connection[Any], seed: dict[str, str]
) -> None:
    aggregate_id = uuid4()
    _insert_event(admin, seed["tenant_a"], aggregate_id=aggregate_id, ordinal=0)
    with pytest.raises(psycopg.errors.UniqueViolation):
        _insert_event(
            admin,
            seed["tenant_a"],
            aggregate_id=aggregate_id,
            ordinal=0,
            event_type="knowledge.version.revoked.v1",
        )
    admin.rollback()


# ---------------------------------------------------------------------------
# 5. 重试预算与死信
# ---------------------------------------------------------------------------
def test_backoff_grows_then_caps() -> None:
    config = RelayConfig(backoff_base_seconds=2.0, backoff_cap_seconds=10.0)
    assert [config.backoff_seconds(n) for n in (1, 2, 3, 4)] == [2.0, 4.0, 8.0, 10.0]
    assert config.backoff_seconds(50) == 10.0


def test_eighth_failure_dead_letters(
    admin: psycopg.Connection[Any], relay_conn: psycopg.Connection[Any], seed: dict[str, str]
) -> None:
    event_id = _insert_event(admin, seed["tenant_a"])
    admin.commit()

    failing = _relay(relay_conn, RecordingTransport(fail=True), max_attempts=8)
    for _ in range(7):
        event = failing.claim(1)[0]
        assert failing.deliver_claimed(event) == "failed"
        _make_due(admin, event_id)

    event = failing.claim(1)[0]
    assert event.retry_count == 8
    assert failing.deliver_claimed(event) == "dead_lettered"

    row = _row(admin, event_id)
    assert row["dead_lettered_at"] is not None
    assert row["published_at"] is None
    assert row["locked_until"] is None and row["delivery_token"] is None
    # 已满次数，不会再被领取
    assert failing.claim(5) == []


def test_crash_after_last_claim_still_ends_as_dead_letter(
    admin: psycopg.Connection[Any], relay_conn: psycopg.Connection[Any], seed: dict[str, str]
) -> None:
    event_id = _insert_event(admin, seed["tenant_a"])
    admin.commit()

    relay = _relay(relay_conn, RecordingTransport(), max_attempts=2, lease_seconds=1)
    for _ in range(2):
        event = relay.claim(1)[0]
        _expire_lease(admin, event_id)
        del event  # 模拟：领取后进程崩溃，既不确认也不失败

    assert relay.recover_expired() == 1
    row = _row(admin, event_id)
    assert row["dead_lettered_at"] is not None
    assert row["locked_until"] is None and row["delivery_token"] is None


# ---------------------------------------------------------------------------
# 6. 封套稳定性
# ---------------------------------------------------------------------------
def _schema_validator() -> Draft202012Validator:
    schema = json.loads((SCHEMA_DIR / "event_envelope.json").read_text(encoding="utf-8"))
    return Draft202012Validator(schema, format_checker=FormatChecker())


def test_envelope_passes_strict_schema_and_falls_back_to_event_id(
    admin: psycopg.Connection[Any], relay_conn: psycopg.Connection[Any], seed: dict[str, str]
) -> None:
    event_id = _insert_event(admin, seed["tenant_a"], trace_id=None)
    admin.commit()

    event = _relay(relay_conn, RecordingTransport()).claim(1)[0]
    envelope = event.envelope("knowledge-api")
    _schema_validator().validate(envelope)
    assert envelope["trace_id"] == str(event_id)


def test_blank_trace_is_rejected_by_database(
    admin: psycopg.Connection[Any], seed: dict[str, str]
) -> None:
    with pytest.raises(psycopg.errors.CheckViolation):
        _insert_event(admin, seed["tenant_a"], trace_id="   ")
    admin.rollback()


def test_retry_preserves_envelope_identity(
    admin: psycopg.Connection[Any], relay_conn: psycopg.Connection[Any], seed: dict[str, str]
) -> None:
    event_id = _insert_event(admin, seed["tenant_a"], trace_id="trace-upstream")
    admin.commit()

    failing = _relay(relay_conn, RecordingTransport(fail=True))
    first = failing.claim(1)[0]
    failing.deliver_claimed(first)
    _make_due(admin, event_id)

    retry = _relay(relay_conn, RecordingTransport()).claim(1)[0]
    assert retry.envelope("knowledge-api") == first.envelope("knowledge-api")


# ---------------------------------------------------------------------------
# 7. 传输故障
# ---------------------------------------------------------------------------
def test_relay_refuses_to_start_without_transport() -> None:
    with pytest.raises(TransportNotConfigured):
        build_transport(Settings(outbox_transport="none", outbox_webhook_url=None))


def test_webhook_status_whitelist_is_explicit(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Response:
        def __init__(self, status: int) -> None:
            self.status = status

        def __enter__(self) -> _Response:
            return self

        def __exit__(self, *args: object) -> None:
            return None

    monkeypatch.setattr(
        "urllib.request.urlopen", lambda request, timeout=None: _Response(201)
    )
    WebhookTransport("http://receiver.test/events").deliver({"event_id": "e-1"})

    def _reject(request: Any, timeout: Any = None) -> Any:
        raise urllib.error.HTTPError("http://receiver.test/events", 404, "not found", {}, None)  # type: ignore[arg-type]

    monkeypatch.setattr("urllib.request.urlopen", _reject)
    with pytest.raises(TransportError):
        WebhookTransport("http://receiver.test/events").deliver({"event_id": "e-1"})


def test_delivery_success_without_ack_is_retried_but_consumer_dedupes(
    admin: psycopg.Connection[Any], relay_conn: psycopg.Connection[Any], seed: dict[str, str]
) -> None:
    event_id = _insert_event(admin, seed["tenant_a"])
    admin.commit()

    transport = RecordingTransport()
    crash = _relay(relay_conn, transport)
    crashed = crash.claim(1)[0]
    transport.deliver(crashed.envelope("knowledge-api"))  # 已发出，但确认前崩溃

    _expire_lease(admin, event_id)

    relay = _relay(relay_conn, transport)
    assert relay.run_once().delivered == 1
    assert len(transport.delivered) == 2  # 确实又发了一次
    assert len(transport.applied) == 1  # 消费者按事件 id 去重，副作用只发生一次


# ---------------------------------------------------------------------------
# 8. 死信可见性与人工重放
# ---------------------------------------------------------------------------
def _make_dead(admin: psycopg.Connection[Any], conn: psycopg.Connection[Any], event_id: UUID) -> None:
    relay = _relay(conn, RecordingTransport(fail=True), max_attempts=1, lease_seconds=60)
    event = relay.claim(1)[0]
    assert relay.deliver_claimed(event) == "dead_lettered"
    assert _row(admin, event_id)["dead_lettered_at"] is not None


def test_dead_letter_listing_marks_blocked_successors(
    admin: psycopg.Connection[Any], relay_conn: psycopg.Connection[Any], seed: dict[str, str]
) -> None:
    aggregate_id = uuid4()
    dead = _insert_event(admin, seed["tenant_a"], aggregate_id=aggregate_id, ordinal=0)
    admin.commit()
    _make_dead(admin, relay_conn, dead)

    # 后继与被阻塞关系必须在死信之后再构造，避免「先造两条再挑一条」的领取顺序不确定
    _insert_event(
        admin, seed["tenant_a"], aggregate_id=aggregate_id, ordinal=1,
        event_type="knowledge.version.revoked.v1",
    )
    empty = _insert_event(admin, seed["tenant_a"], aggregate_id=uuid4())
    admin.commit()
    _make_dead(admin, relay_conn, empty)

    listed = {entry.event_id: entry for entry in dead_letters(admin)}
    assert listed[dead].blocks_successors is True
    assert listed[empty].blocks_successors is False
    assert dead_letter_count(admin) == 2


def test_replay_resets_dead_letter_and_removes_blockage(
    admin: psycopg.Connection[Any], relay_conn: psycopg.Connection[Any], seed: dict[str, str]
) -> None:
    aggregate_id = uuid4()
    dead = _insert_event(admin, seed["tenant_a"], aggregate_id=aggregate_id, ordinal=0)
    actor = UUID(seed["user_a"])
    admin.commit()
    _make_dead(admin, relay_conn, dead)

    successor = _insert_event(
        admin, seed["tenant_a"], aggregate_id=aggregate_id, ordinal=1,
        event_type="knowledge.version.revoked.v1",
    )
    admin.commit()

    replay_dead_letter(
        relay_conn, tenant_id=UUID(seed["tenant_a"]), event_id=dead, actor_id=actor, reason="接收端已恢复"
    )

    row = _row(admin, dead)
    assert row["dead_lettered_at"] is None and row["retry_count"] == 0

    transport = RecordingTransport()
    claimed = _relay(relay_conn, transport).claim(2)
    assert [event.id for event in claimed] == [dead]
    assert successor not in {event.id for event in claimed}

    audit = admin.execute(
        "SELECT actor_id, reason FROM outbox_replays WHERE event_id = %s", (dead,)
    ).fetchall()
    admin.rollback()
    assert [(str(row[0]), row[1]) for row in audit] == [(str(actor), "接收端已恢复")]


def test_replay_rejects_published_event(
    admin: psycopg.Connection[Any], relay_conn: psycopg.Connection[Any], seed: dict[str, str]
) -> None:
    event_id = _insert_event(admin, seed["tenant_a"])
    admin.commit()
    relay = _relay(relay_conn, RecordingTransport())
    relay.deliver_claimed(relay.claim(1)[0])

    with pytest.raises(OutboxReplayRejected):
        replay_dead_letter(
            relay_conn,
            tenant_id=UUID(seed["tenant_a"]),
            event_id=event_id,
            actor_id=UUID(seed["user_a"]),
            reason="误操作",
        )


def test_replay_audit_is_append_only(
    admin: psycopg.Connection[Any], relay_conn: psycopg.Connection[Any], seed: dict[str, str]
) -> None:
    event_id = _insert_event(admin, seed["tenant_a"])
    admin.commit()
    _make_dead(admin, relay_conn, event_id)
    replay_dead_letter(
        relay_conn,
        tenant_id=UUID(seed["tenant_a"]),
        event_id=event_id,
        actor_id=UUID(seed["user_a"]),
        reason="接收端已恢复",
    )

    # 触发器在超级用户下也必须拦住改写与删除（绕过权限也绕不过审计约束）
    with pytest.raises(psycopg.errors.CheckViolation):
        admin.execute("UPDATE outbox_replays SET reason = '改一下'")
    admin.rollback()

    with pytest.raises(psycopg.errors.CheckViolation):
        admin.execute("DELETE FROM outbox_replays")
    admin.rollback()

    # relay 角色本身连改写权限都没有：审计表对它是只写
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        relay_conn.execute("UPDATE outbox_replays SET reason = '改一下'")
    relay_conn.rollback()
