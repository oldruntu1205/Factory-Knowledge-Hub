"""数据库连接与请求级依赖。

池在应用 lifespan 中创建：数据库未配置时池为 None，业务端点返回 503，
与 /health/ready 的判断保持一致。这样"没配数据库"不会表现为某个端点神秘 500。
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import psycopg
from fastapi import Request
from psycopg_pool import ConnectionPool

from fkh.api.errors import ApiError, ErrorCode


def create_pool(dsn: str, *, min_size: int = 1, max_size: int = 5) -> ConnectionPool[Any]:
    return ConnectionPool(dsn, min_size=min_size, max_size=max_size, open=True, timeout=5)


def get_pool(request: Request) -> ConnectionPool[Any] | None:
    return getattr(request.app.state, "pool", None)


def get_conn(request: Request) -> Iterator[psycopg.Connection[Any]]:
    pool = get_pool(request)
    if pool is None:
        raise ApiError(ErrorCode.SERVICE_UNAVAILABLE, detail="数据库未配置，业务端点不可用")
    # 退出上下文时提交；异常时回滚，事务边界即请求边界
    with pool.connection() as conn:
        yield conn
