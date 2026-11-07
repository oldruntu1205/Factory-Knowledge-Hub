"""`tenant_connection` 生命周期验收（L1 2026-11-07 放行条件 4 的四条子要求）。

真实连接池 + 真实 PostgreSQL：
1. 借出后立刻绑定租户（业务 SQL 前）；
2. 正常 / 异常 / 取消路径都清理；
3. 清理在独立提交里完成，不会被后续 rollback 撤销；
4. 清理失败时丢弃连接，不把残留租户的连接还回池子。
"""

from __future__ import annotations

import pytest

from fkh.api.db import tenant_connection

pytestmark = pytest.mark.requires_pg

import uuid  # noqa: E402

import psycopg  # noqa: E402
from psycopg_pool import ConnectionPool  # noqa: E402


def _guc(conn: psycopg.Connection[object]) -> str | None:
    return conn.execute("SELECT current_setting('app.tenant_id', true)").fetchone()[0]


@pytest.fixture
def pool(pg_url: str) -> ConnectionPool[object]:
    with ConnectionPool(pg_url, min_size=1, max_size=1, open=True) as created:
        yield created


def test_tenant_bound_before_business_sql_and_cleared_after(
    pool: ConnectionPool[object], admin: psycopg.Connection[object]
) -> None:
    tenant = admin.execute("SELECT id FROM tenants LIMIT 1").fetchone()
    assert tenant is not None
    tenant_id = tenant[0]

    with tenant_connection(pool, tenant_id) as conn:
        # 业务 SQL 之前就已经能看到租户已绑定
        assert _guc(conn) == str(tenant_id)
        conn.execute("SELECT count(*) FROM knowledge_chunks").fetchone()

    # 归还后必须干净
    with pool.connection() as probe:
        assert _guc(probe) in (None, ""), "连接归还池子时仍带着租户上下文"


def test_cleanup_runs_on_exception(
    pool: ConnectionPool[object], admin: psycopg.Connection[object]
) -> None:
    tenant_id = admin.execute("SELECT id FROM tenants LIMIT 1").fetchone()[0]
    with pytest.raises(RuntimeError), tenant_connection(pool, tenant_id) as conn:
        assert _guc(conn) == str(tenant_id)
        raise RuntimeError("business failure")

    with pool.connection() as probe:
        assert _guc(probe) in (None, "")


def test_cleanup_survives_rollback_of_business_transaction(
    pool: ConnectionPool[object], admin: psycopg.Connection[object]
) -> None:
    """业务事务失败（被 abort）后，清理仍然生效——不能被 rollback 撤销。"""
    tenant_id = admin.execute("SELECT id FROM tenants LIMIT 1").fetchone()[0]
    with pytest.raises(psycopg.errors.UndefinedTable), tenant_connection(pool, tenant_id) as conn:
        conn.execute("SELECT * FROM table_that_does_not_exist").fetchone()

    with pool.connection() as probe:
        assert _guc(probe) in (None, ""), "abort 事务后的回滚把清理也撤掉了"


def test_connection_is_discarded_when_cleanup_fails(
    pool: ConnectionPool[object], admin: psycopg.Connection[object], monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant_id = admin.execute("SELECT id FROM tenants LIMIT 1").fetchone()[0]

    with tenant_connection(pool, tenant_id) as conn:
        inner_id = id(conn)
        # 让后续清理语句必然失败
        conn.close()

    with pool.connection() as probe:
        assert id(probe) != inner_id, "清理失败后不得复用同一条连接"
        assert not probe.closed


def test_tenant_context_does_not_leak_between_two_tenants(
    pool: ConnectionPool[object], admin: psycopg.Connection[object]
) -> None:
    rows = admin.execute("SELECT id FROM tenants LIMIT 2").fetchall()
    if len(rows) < 2:
        pytest.skip("需要至少两个租户")
    first, second = (str(row[0]) for row in rows)

    with tenant_connection(pool, uuid.UUID(first)) as conn:
        assert _guc(conn) == first
    with tenant_connection(pool, uuid.UUID(second)) as conn:
        assert _guc(conn) == second
    with pool.connection() as probe:
        assert _guc(probe) in (None, ""), "第二个租户结束后仍残留上下文"


def test_cancel_style_baseexception_still_discards_connection(
    pool: ConnectionPool[object], admin: psycopg.Connection[object]
) -> None:
    """取消类 BaseException（如 CancelledError/KeyboardInterrupt）也必须丢弃连接（L1 边界）。"""
    tenant_id = admin.execute("SELECT id FROM tenants LIMIT 1").fetchone()[0]

    with pytest.raises(KeyboardInterrupt), tenant_connection(pool, tenant_id) as conn:
        inner_id = id(conn)
        conn.close()  # 让清理阶段抛错，同时模拟取消
        raise KeyboardInterrupt

    with pool.connection() as probe:
        assert id(probe) != inner_id
        assert not probe.closed
