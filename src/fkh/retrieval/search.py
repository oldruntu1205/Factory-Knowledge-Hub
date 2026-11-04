"""检索编排：范围授权 → 召回（纯词法 / 双路混合）→ 审计。

审计要点（L1 判决）：
- query 只以服务端密钥的 HMAC 落库，明文不进表、不进日志、不进异常消息；
- 成功（含空结果）、范围未授权、执行失败三类都留痕；
- 成功路径**先提交审计再返回结果**：审计写不进去就返回服务错误，不给"查过但没记录"的机会；
- 不记录候选数与「全部被过滤」标志——那是能被用来推断无权内容存在性的元数据；
- 记录**本次实际执行的召回模式**（2026-11-04 判决 Q3）。未执行召回就被拒绝的记录写
  `not_executed`，不得伪记成执行过某种模式。

召回模式（2026-11-04 判决 Q2 / Q7）：
- 默认 `lexical`：纯词法，行为与接线前完全一致；
- `hybrid`：服务端显式 opt-in 后启用向量+词法 RRF 融合；缺模型或推理失败一律 fail-closed 返回
  503，**不静默降级**成词法结果；
- `/v1/knowledge/context` 明确固定为词法（判决 Q7），候选来源变化会改变拒答分档，须另行送审。
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
from fkh.retrieval.embedding import Embedder, EmbeddingError
from fkh.retrieval.hybrid import HybridRetriever
from fkh.retrieval.lexical import SCORING as LEXICAL_SCORING
from fkh.retrieval.lexical import LexicalRetriever, SearchHit, SearchOutcome

REFUSAL_SCOPE_NOT_AUTHORIZED = "SCOPE_NOT_AUTHORIZED"
ERROR_EXECUTION_FAILED = "EXECUTION_FAILED"
ERROR_EMBEDDING_UNAVAILABLE = "EMBEDDING_UNAVAILABLE"

# 召回模式 → 响应里的 scoring 取值。两者必须一一对应：响应要如实标识实际执行的算法。
LEXICAL_MODE = "lexical"
HYBRID_MODE = "hybrid"
# 词法的 scoring 取自 lexical.SCORING（lexical-v2），避免两处字符串各自漂移。
SCORING_BY_MODE = {LEXICAL_MODE: LEXICAL_SCORING, HYBRID_MODE: "hybrid-rrf-v1"}
# 审计里表示「召回前就被拒绝、没有执行任何算法」的取值（与 scoring 枚举同表，见迁移 0011）
MODE_NOT_EXECUTED = "not_executed"


class SearchScopeNotAuthorized(PermissionError):
    """令牌里没有请求的 knowledge_scope。必须拒绝，不能伪装成空结果。"""


class SearchNotConfigured(RuntimeError):
    """检索能力未配置齐全：审计密钥缺失、检索模式非法、混合模式缺模型。"""


class SearchUnavailable(RuntimeError):
    """已配置的能力在执行期失败（模型权重缺失、推理异常）。与"未配置"分开，但同样是 503。"""


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


@dataclass(frozen=True, slots=True)
class SearchResult:
    """检索结果 + 本次实际执行的算法标识（响应必须如实回给调用方）。"""

    hits: tuple[SearchHit, ...]
    scoring: str


_AUDIT_SQL = """
INSERT INTO retrieval_queries
  (tenant_id, principal_id, query_hash, query_hash_key_id, knowledge_scope,
   status, refusal_code, returned_count, trace_id, duration_ms, retrieval_mode)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""


class SearchService:
    def __init__(
        self,
        conn: psycopg.Connection[Any],
        *,
        hash_key: str | None,
        hash_key_id: str,
        embedder: Embedder | None = None,
        rrf_k: int = 60,
    ) -> None:
        self._conn = conn
        self._hash_key = hash_key
        self._hash_key_id = hash_key_id
        self._embedder = embedder
        self._rrf_k = rrf_k

    def search(
        self,
        query: str,
        *,
        knowledge_scope: str,
        max_results: int,
        context: SearchContext,
        retrieval_mode: str = LEXICAL_MODE,
    ) -> SearchResult:
        if not self._hash_key:
            # 没有密钥就写不了合规审计；此时宁可不可用，也不要落可反推的哈希
            raise SearchNotConfigured("未配置 FKH_RETRIEVAL_HASH_KEY")

        fingerprint = query_fingerprint(self._hash_key, query)
        started = time.monotonic()

        scoring = SCORING_BY_MODE.get(retrieval_mode)
        if scoring is None:
            # 配置写错不能退化成词法：那会让"以为开了混合"与"实际纯词法"无法区分
            self._audit_best_effort_error(
                fingerprint, knowledge_scope, context, started,
                refusal_code=ERROR_EXECUTION_FAILED, retrieval_mode=MODE_NOT_EXECUTED,
            )
            raise SearchNotConfigured(f"未知检索模式：{retrieval_mode}")

        if retrieval_mode == HYBRID_MODE and self._embedder is None:
            # 显式启用了混合却没有模型：fail-closed（判决 Q2），不静默降级
            self._audit_best_effort_error(
                fingerprint, knowledge_scope, context, started,
                refusal_code=ERROR_EMBEDDING_UNAVAILABLE, retrieval_mode=MODE_NOT_EXECUTED,
            )
            raise SearchNotConfigured("混合检索已启用，但 embedding 模型未配置")

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
                retrieval_mode=MODE_NOT_EXECUTED,
            )
            self._conn.commit()
            raise SearchScopeNotAuthorized(knowledge_scope)

        try:
            outcome = self._recall(
                query,
                tenant_id=context.tenant_id,
                knowledge_scope=knowledge_scope,
                subject=context.subject,
                max_results=max_results,
                retrieval_mode=retrieval_mode,
            )
        except EmbeddingError as exc:
            self._audit_best_effort_error(
                fingerprint, knowledge_scope, context, started,
                refusal_code=ERROR_EMBEDDING_UNAVAILABLE, retrieval_mode=scoring,
            )
            raise SearchUnavailable(str(exc)) from exc
        except Exception:
            self._audit_best_effort_error(
                fingerprint, knowledge_scope, context, started,
                refusal_code=ERROR_EXECUTION_FAILED, retrieval_mode=scoring,
            )
            raise

        self._audit(
            fingerprint=fingerprint,
            scope=knowledge_scope,
            status="SUCCEEDED",
            refusal_code=None,
            returned_count=len(outcome.hits),
            context=context,
            duration_ms=_elapsed_ms(started),
            retrieval_mode=scoring,
        )
        # 审计先于响应：请求级连接要到响应之后才提交，不能依赖它
        self._conn.commit()
        return SearchResult(hits=outcome.hits, scoring=scoring)

    def _recall(
        self,
        query: str,
        *,
        tenant_id: UUID,
        knowledge_scope: str,
        subject: Subject,
        max_results: int,
        retrieval_mode: str,
    ) -> SearchOutcome:
        if retrieval_mode == HYBRID_MODE:
            if self._embedder is None:  # 理论上前面已拦，保留不变式
                raise SearchNotConfigured("混合检索已启用，但 embedding 模型未配置")
            return HybridRetriever(
                self._conn, tenant_id, embedder=self._embedder, rrf_k=self._rrf_k
            ).search(
                query,
                knowledge_scope=knowledge_scope,
                subject=subject,
                max_results=max_results,
            )
        return LexicalRetriever(self._conn, tenant_id).search(
            query,
            knowledge_scope=knowledge_scope,
            subject=subject,
            max_results=max_results,
        )

    def _audit_best_effort_error(
        self,
        fingerprint: str,
        scope: str,
        context: SearchContext,
        started: float,
        *,
        refusal_code: str,
        retrieval_mode: str,
    ) -> None:
        """执行失败也要留痕，但审计本身再失败不能掩盖原始异常。"""
        try:
            self._audit(
                fingerprint=fingerprint,
                scope=scope,
                status="ERROR",
                refusal_code=refusal_code,
                returned_count=None,
                context=context,
                duration_ms=_elapsed_ms(started),
                retrieval_mode=retrieval_mode,
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
        retrieval_mode: str,
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
                retrieval_mode,
            ),
        )


def _elapsed_ms(started: float) -> int:
    return max(0, int((time.monotonic() - started) * 1000))


__all__ = [
    "ERROR_EMBEDDING_UNAVAILABLE",
    "ERROR_EXECUTION_FAILED",
    "HYBRID_MODE",
    "LEXICAL_MODE",
    "MODE_NOT_EXECUTED",
    "REFUSAL_SCOPE_NOT_AUTHORIZED",
    "SCORING_BY_MODE",
    "SearchContext",
    "SearchNotConfigured",
    "SearchResult",
    "SearchScopeNotAuthorized",
    "SearchService",
    "SearchUnavailable",
    "query_fingerprint",
]

