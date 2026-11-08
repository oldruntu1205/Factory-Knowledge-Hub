"""数据库连接与请求级依赖。

池在应用 lifespan 中创建：数据库未配置时池为 None，业务端点返回 503，
与 /health/ready 的判断保持一致。这样"没配数据库"不会表现为某个端点神秘 500。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterator, MutableMapping
from contextlib import contextmanager
from typing import Any

import psycopg
from fastapi import Request
from psycopg_pool import ConnectionPool
from starlette.concurrency import run_in_threadpool
from starlette.types import Message, Receive, Scope, Send

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


def _commit_before_send(conn: psycopg.Connection[Any]) -> None:
    if not conn.closed:
        conn.commit()


def _rollback_before_send(conn: psycopg.Connection[Any]) -> None:
    if not conn.closed:
        conn.rollback()


class CommitBeforeSend:
    """在响应字节发出**之前**提交本请求的事务。

    FastAPI 的 yield 依赖在 `await response(...)` 之后才执行退出逻辑
    （`fastapi/routing.py` 的 request_response：先发送响应，再关 request exit stack）。
    于是"数据库已提交"实际发生在客户端收到 201 之后：客户端立刻发起的下一条请求
    可能读不到刚写入的行（2026-11-09 实测：上传 201 后 3.9ms 的审核 403，行在响应后
    158ms 才可见；租户绑定正常，这是事务可见性竞态）。

    这里包在 ASGI 发送链上：第一条 `http.response.start` 到达时，成功响应提交、
    错误响应（>=400）显式回滚，完成之后才放行响应。依赖退出时仍会做一次收敛
    （成功 commit / 异常 rollback），但不再是唯一的提交点。
    """

    def __init__(self, app: Callable[[Scope, Receive, Send], Awaitable[None]]) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        state: MutableMapping[str, Any] = scope.setdefault("state", {})
        committed = False

        async def send_wrapper(message: Message) -> None:
            nonlocal committed
            if not committed and message["type"] == "http.response.start":
                committed = True
                conn = state.get("conn")
                status = int(message.get("status", 500))
                if conn is not None:
                    # 成功：提交事务，保证客户端收到 2xx/3xx 时数据已可见；
                    # 错误：显式回滚，不依赖"端点一定抛异常"这一隐含假设
                    # （端点若正常返回 4xx Response，yield 依赖会正常结束并提交）。
                    # commit/rollback 是阻塞 IO：放线程池，避免卡住事件循环。
                    if status < 400:
                        await run_in_threadpool(_commit_before_send, conn)
                    else:
                        await run_in_threadpool(_rollback_before_send, conn)
            await send(message)

        await self.app(scope, receive, send_wrapper)


def get_conn(
    request: Request, context: PrincipalDep
) -> Iterator[psycopg.Connection[Any]]:
    pool = get_pool(request)
    if pool is None:
        raise ApiError(ErrorCode.SERVICE_UNAVAILABLE, detail="数据库未配置，业务端点不可用")
    # 退出上下文时提交；异常时回滚，事务边界即请求边界。
    # 连接同时挂到 request.state，供 CommitBeforeSend 在响应发出前提交。
    with tenant_connection(pool, context.tenant_id) as conn:
        request.state.conn = conn
        yield conn
