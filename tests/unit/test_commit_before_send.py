"""响应前提交事务的回归：成功响应必须在首个 send 之前提交，错误响应不得提交。"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from fkh.api import db


class _FakeConn:
    def __init__(self, *, fail: bool = False) -> None:
        self.closed = False
        self.commits = 0
        self.rollbacks = 0
        self._fail = fail

    def commit(self) -> None:
        if self._fail:
            raise RuntimeError("commit failed")
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


def _inline_threadpool(monkeypatch: pytest.MonkeyPatch) -> None:
    async def inline(func: Any, *args: Any) -> Any:
        return func(*args)

    monkeypatch.setattr(db, "run_in_threadpool", inline)


def test_success_response_commits_before_start(monkeypatch: pytest.MonkeyPatch) -> None:
    _inline_threadpool(monkeypatch)
    conn = _FakeConn()
    seen: list[tuple[str, int]] = []

    async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
        scope["state"]["conn"] = conn
        await send({"type": "http.response.start", "status": 201, "headers": []})
        await send({"type": "http.response.body", "body": b"x"})

    async def send(message: dict[str, Any]) -> None:
        seen.append((message["type"], conn.commits))

    asyncio.run(db.CommitBeforeSend(app)({"type": "http", "state": {}}, None, send))

    assert conn.commits == 1
    assert seen[0] == ("http.response.start", 1)
    assert seen[1] == ("http.response.body", 1)


def test_error_response_does_not_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    _inline_threadpool(monkeypatch)
    conn = _FakeConn()
    seen: list[str] = []

    async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
        scope["state"]["conn"] = conn
        await send({"type": "http.response.start", "status": 403, "headers": []})

    async def send(message: dict[str, Any]) -> None:
        seen.append(message["type"])

    asyncio.run(db.CommitBeforeSend(app)({"type": "http", "state": {}}, None, send))

    assert conn.commits == 0
    assert conn.rollbacks == 1
    assert seen == ["http.response.start"]


def test_commit_failure_blocks_response(monkeypatch: pytest.MonkeyPatch) -> None:
    _inline_threadpool(monkeypatch)
    conn = _FakeConn(fail=True)
    seen: list[str] = []

    async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
        scope["state"]["conn"] = conn
        await send({"type": "http.response.start", "status": 200, "headers": []})

    async def send(message: dict[str, Any]) -> None:
        seen.append(message["type"])

    with pytest.raises(RuntimeError, match="commit failed"):
        asyncio.run(db.CommitBeforeSend(app)({"type": "http", "state": {}}, None, send))
    assert seen == []


def test_non_http_scope_passthrough(monkeypatch: pytest.MonkeyPatch) -> None:
    _inline_threadpool(monkeypatch)
    calls: list[str] = []

    async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
        calls.append(scope["type"])

    async def send(message: dict[str, Any]) -> None:
        raise AssertionError("non-http 不应发送响应")

    asyncio.run(db.CommitBeforeSend(app)({"type": "lifespan", "state": {}}, None, send))
    assert calls == ["lifespan"]
