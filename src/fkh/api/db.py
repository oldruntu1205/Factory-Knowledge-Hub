"""数据库连接与请求级依赖。

池在应用 lifespan 中创建：数据库未配置时池为 None，业务端点返回 503，
与 /health/ready 的判断保持一致。这样"没配数据库"不会表现为某个端点神秘 500。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import psycopg
from fastapi import Request
from psycopg_pool import ConnectionPool

from fkh.api.errors import ApiError, ErrorCode
from fkh.api.security import PrincipalDep


def create_pool(dsn: str, *, min_size: int = 1, max_size: int = 5) -> ConnectionPool[Any]:
    return ConnectionPool(dsn, min_size=min_size, max_size=max_size, open=True, timeout=5)


def get_pool(request: Request) -> ConnectionPool[Any] | None:
    return getattr(request.app.state, "pool", None)


def bind_tenant(conn: psycopg.Connection[Any], tenant_id: Any) -> None:
    """把租户绑定到**会话级** GUC。

    为什么不是 `is_local=true`：检索/审计路径里有显式 commit（审计必须先落库），
    事务一结束，事务级 GUC 就消失——随后同请求内的写入会撞 RLS 的 WITH CHECK。
    owner 连接会绕过 RLS，所以这个缺陷只在业务角色下暴露（2026-11-07 实测）。

    会话级设置的代价是连接复用可能串租户，因此必须在归还连接前 clear。
    """
    conn.execute("SELECT set_config('app.tenant_id', %s, false)", (str(tenant_id),))


def clear_tenant(conn: psycopg.Connection[Any]) -> None:
    conn.execute("SELECT set_config('app.tenant_id', '', false)")


@contextmanager
def tenant_connection(
    pool: ConnectionPool[Any], tenant_id: Any
) -> Iterator[psycopg.Connection[Any]]:
    """**唯一**允许从连接池取连接的入口（L1 2026-11-07 放行门禁）。

    契约：
    1. 借出后、任何业务 SQL 之前绑定可信租户（会话级，commit 不会丢）；
    2. 正常 / 异常 / 取消路径都执行清理；
    3. 清理在**独立提交**里完成，不会被后续 rollback 撤销；
    4. 清理失败即丢弃连接（关闭），绝不把带残留租户的连接还回池子。
    """
    with pool.connection() as conn:
        bind_tenant(conn, tenant_id)
        completed = False
        try:
            yield conn
            completed = True
        finally:
            _release(conn, completed=completed)


def _release(conn: psycopg.Connection[Any], *, completed: bool) -> None:
    """归还前的收尾：先结算业务事务，再单开事务清租户；任何一步失败就丢弃连接。"""
    try:
        status = conn.info.transaction_status
        if status == psycopg.pq.TransactionStatus.INERROR:
            conn.rollback()  # abort 的事务必须先回滚，否则清理语句会被拒绝
        elif status != psycopg.pq.TransactionStatus.IDLE:
            if completed:
                conn.commit()  # 业务成功：保住它的工作
            else:
                conn.rollback()  # 业务抛异常：整体回滚
        conn.execute("SELECT set_config('app.tenant_id', '', false)")
        conn.commit()  # 清理单独提交，不被后续 rollback 撤销
    except BaseException as exc:  # noqa: BLE001 - 清理失败必须丢弃连接
        # 取消（CancelledError/KeyboardInterrupt 等 BaseException）同样要丢弃连接：
        # 只捕 Exception 会让"清理被中断"留下带租户的连接（L1 2026-11-07 边界提醒）。
        conn.close()
        if not isinstance(exc, Exception):
            raise


def get_conn(
    request: Request, context: PrincipalDep
) -> Iterator[psycopg.Connection[Any]]:
    pool = get_pool(request)
    if pool is None:
        raise ApiError(ErrorCode.SERVICE_UNAVAILABLE, detail="数据库未配置，业务端点不可用")
    # 退出上下文时提交；异常时回滚，事务边界即请求边界
    with tenant_connection(pool, context.tenant_id) as conn:
        yield conn
