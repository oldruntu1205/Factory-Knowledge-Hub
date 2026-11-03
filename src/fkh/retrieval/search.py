"""检索编排：范围授权 → 召回 → 审计。

审计要点（L1 判决第 5 条）：
- query 只以服务端密钥的 HMAC 落库，明文不进表、不进日志、不进异常消息；
- 成功（含空结果）、范围未授权、执行失败三类都留痕；
- 成功路径**先提交审计再返回结果**：审计写不进去就返回服务错误，不给"查过但没记录"的机会；
- 不记录候选数与「全部被过滤」标志——那是能被用来推断无权内容存在性的元数据。
"""

from __future__ import annotations

import hashlib
import hmac
import time
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import psycopg

from fkh.authz.policy import Subject
from fkh.retrieval.lexical import LexicalRetriever, SearchHit

REFUSAL_SCOPE_NOT_AUTHORIZED = "SCOPE_NOT_AUTHORIZED"
ERROR_EXECUTION_FAILED = "EXECUTION_FAILED"


class SearchScopeNotAuthorized(PermissionError):
    """令牌里没有请求的 knowledge_scope。必须拒绝，不能伪装成空结果。"""


class SearchNotConfigured(RuntimeError):
    """未配置审计用的服务端密钥。没有它就无法在不落明文的前提下记录检索。"""


def query_fingerprint(key: str, query: str) -> str:
    """HMAC-SHA256：普通哈希对短查询可被字典反推，等于绕开「禁止落明文」。"""
    return hmac.new(key.encode("utf-8"), query.encode("utf-8"), hashlib.sha256).hexdigest()


@dataclass(frozen=True, slots=True)
class SearchContext:
    tenant_id: UUID
    principal_id: UUID
    subject: Subject
    allowed_scopes: frozenset[str]
    trace_id: str


_AUDIT_SQL = """
INSERT INTO retrieval_queries
  (tenant_id, principal_id, query_hash, query_hash_key_id, knowledge_scope,
   status, refusal_code, returned_count, trace_id, duration_ms)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""


class SearchService:
    def __init__(
        self,
        conn: psycopg.Connection[Any],
        *,
        hash_key: str | None,
        hash_key_id: str,
    ) -> None:
        self._conn = conn
        self._hash_key = hash_key
        self._hash_key_id = hash_key_id

    def search(
        self,
        query: str,
        *,
        knowledge_scope: str,
        max_results: int,
        context: SearchContext,
    ) -> tuple[SearchHit, ...]:
        if not self._hash_key:
            # 没有密钥就写不了合规审计；此时宁可不可用，也不要落可反推的哈希
            raise SearchNotConfigured("未配置 FKH_RETRIEVAL_HASH_KEY")

        fingerprint = query_fingerprint(self._hash_key, query)
        started = time.monotonic()

        if knowledge_scope not in context.allowed_scopes:
            # 范围授权与数据可见性分开：接口权限不等于数据范围权限
            self._audit(
                fingerprint=fingerprint,
                scope=knowledge_scope,
                status="REFUSED",
                refusal_code=REFUSAL_SCOPE_NOT_AUTHORIZED,
                returned_count=None,
                context=context,
                duration_ms=_elapsed_ms(started),
            )
            self._conn.commit()
            raise SearchScopeNotAuthorized(knowledge_scope)

        try:
            outcome = LexicalRetriever(self._conn, context.tenant_id).search(
                query,
                knowledge_scope=knowledge_scope,
                subject=context.subject,
                max_results=max_results,
            )
        except Exception:
            self._audit_best_effort_error(fingerprint, knowledge_scope, context, started)
            raise

        self._audit(
            fingerprint=fingerprint,
            scope=knowledge_scope,
            status="SUCCEEDED",
            refusal_code=None,
            returned_count=len(outcome.hits),
            context=context,
            duration_ms=_elapsed_ms(started),
        )
        # 审计先于响应：请求级连接要到响应之后才提交，不能依赖它
        self._conn.commit()
        return outcome.hits

    def _audit_best_effort_error(
        self,
        fingerprint: str,
        scope: str,
        context: SearchContext,
        started: float,
    ) -> None:
        """执行失败也要留痕，但审计本身再失败不能掩盖原始异常。"""
        try:
            self._audit(
                fingerprint=fingerprint,
                scope=scope,
                status="ERROR",
                refusal_code=ERROR_EXECUTION_FAILED,
                returned_count=None,
                context=context,
                duration_ms=_elapsed_ms(started),
            )
            self._conn.commit()
        except psycopg.Error:
            self._conn.rollback()

    def _audit(
        self,
        *,
        fingerprint: str,
        scope: str,
        status: str,
        refusal_code: str | None,
        returned_count: int | None,
        context: SearchContext,
        duration_ms: int,
    ) -> None:
        self._conn.execute(
            _AUDIT_SQL,
            (
                context.tenant_id,
                context.principal_id,
                fingerprint,
                self._hash_key_id,
                scope,
                status,
                refusal_code,
                returned_count,
                context.trace_id,
                duration_ms,
            ),
        )


def _elapsed_ms(started: float) -> int:
    return max(0, int((time.monotonic() - started) * 1000))


__all__ = [
    "ERROR_EXECUTION_FAILED",
    "REFUSAL_SCOPE_NOT_AUTHORIZED",
    "SearchContext",
    "SearchNotConfigured",
    "SearchScopeNotAuthorized",
    "SearchService",
    "query_fingerprint",
]
