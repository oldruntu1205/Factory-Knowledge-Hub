"""培训正文分页游标（L1 2026-11-07：base64 不提供防篡改能力）。

游标是**服务端签名的位置声明**，不是"客户端选的偏移量"：

- 绑定 `tenant / subject / assignment / course_version / 位置`；
- 换任务、换主体、课程修订变更（重发布/撤销后转 NEEDS_REVIEW）→ 验签失败或声明过期 → 拒绝；
- 篡改任意字节 → HMAC 不匹配 → 拒绝；
- 带过期时间，旧游标不保留撤销前的读取资格。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from typing import Any

CURSOR_VERSION = 1
DEFAULT_TTL_SECONDS = 600
MAX_TTL_SECONDS = 3600


class CursorError(ValueError):
    """游标不可信：篡改、跨主体/跨任务复用、过期、版本不符。"""


@dataclass(frozen=True, slots=True)
class CursorState:
    tenant_id: str
    subject_id: str
    assignment_id: str
    course_version: int
    source_ordinal: int
    block_ordinal: int


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64d(text: str) -> bytes:
    padded = text + "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(padded.encode())


def _signature(key: str, body: str) -> str:
    return _b64e(hmac.new(key.encode(), body.encode(), hashlib.sha256).digest())


def encode_cursor(key: str, state: CursorState, *, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> str:
    ttl = max(1, min(ttl_seconds, MAX_TTL_SECONDS))
    payload: dict[str, Any] = {
        "v": CURSOR_VERSION,
        "t": state.tenant_id,
        "s": state.subject_id,
        "a": state.assignment_id,
        "cv": state.course_version,
        "so": state.source_ordinal,
        "bo": state.block_ordinal,
        "exp": int(time.time()) + ttl,
    }
    body = _b64e(json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode())
    return f"{body}.{_signature(key, body)}"


def decode_cursor(key: str, token: str) -> CursorState:
    try:
        body, signature = token.split(".", 1)
    except ValueError as exc:
        raise CursorError("游标格式非法") from exc
    expected = _signature(key, body)
    if not hmac.compare_digest(expected, signature):
        raise CursorError("游标签名不匹配")
    try:
        payload = json.loads(_b64d(body))
    except (ValueError, json.JSONDecodeError) as exc:
        raise CursorError("游标内容不可解析") from exc
    if int(payload.get("v", 0)) != CURSOR_VERSION:
        raise CursorError("游标版本不受支持")
    if int(payload.get("exp", 0)) < int(time.time()):
        raise CursorError("游标已过期")
    try:
        return CursorState(
            tenant_id=str(payload["t"]),
            subject_id=str(payload["s"]),
            assignment_id=str(payload["a"]),
            course_version=int(payload["cv"]),
            source_ordinal=int(payload["so"]),
            block_ordinal=int(payload["bo"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise CursorError("游标字段缺失") from exc


__all__ = [
    "CURSOR_VERSION",
    "CursorError",
    "CursorState",
    "decode_cursor",
    "encode_cursor",
]
