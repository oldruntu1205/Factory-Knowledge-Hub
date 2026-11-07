"""问答执行器（HTTP 层共用）。

`/v1/knowledge/answer` 与 `/v1/training/ask` 走的是**同一条**闸门：
同样的检索、同样的引用白名单、同样的 `literal_support` 一致性检查。
差别只在于知识范围（scope）与调用前的业务前置校验（培训要先确认这是本人任务）。

把执行逻辑放这里的目的：两条通路不可能出现"培训版少做了一道检查"。
"""

from __future__ import annotations

from typing import Any, Protocol

import psycopg

from fkh.answer import AnswerNotConfigured, AnswerService
from fkh.api.errors import ApiError, ErrorCode
from fkh.config import Settings
from fkh.generation import GenerationError, GenerationNotConfigured, get_generator
from fkh.retrieval.embedding import EmbeddingNotConfigured
from fkh.retrieval.search import (
    SearchContext,
    SearchNotConfigured,
    SearchScopeNotAuthorized,
    SearchUnavailable,
)

# 这些异常都表示"能力未就绪/不可达"，必须 503；不能伪装成业务拒答（Rev2 §4.5）。
_UNAVAILABLE = (SearchNotConfigured, SearchUnavailable, AnswerNotConfigured, GenerationError)


class PrincipalView(Protocol):
    """执行器需要的最小身份视图（`api.security.PrincipalContext` 结构上满足）。"""

    @property
    def tenant_id(self) -> Any: ...

    @property
    def principal_id(self) -> Any: ...

    @property
    def allowed_scopes(self) -> frozenset[str]: ...

    def to_subject(self) -> Any: ...


def run_answer(
    conn: psycopg.Connection[Any],
    *,
    settings: Settings,
    context: PrincipalView,
    query: str,
    knowledge_scope: str,
    language: str,
    max_evidence: int,
    trace_id: str,
    embedder: Any = None,
    retrieval_mode: str | None = None,
) -> dict[str, Any]:
    """执行一次问答，返回契约响应体（dict）。

    调用方负责：scope 授权前置检查（越权必须 403，不能被"配置没配好"盖成 503）。
    """
    try:
        generator = get_generator(
            settings.llm_provider,
            base_url=settings.llm_base_url,
            model=settings.llm_model,
            timeout_seconds=settings.llm_timeout_seconds,
        )
    except (GenerationNotConfigured, GenerationError) as exc:
        # 构造期探测失败抛的是 GenerationError，不捕获会变成 500
        raise ApiError(ErrorCode.SERVICE_UNAVAILABLE, detail=str(exc)) from exc

    try:
        service = AnswerService(
            conn,
            generator=generator,
            hash_key=settings.retrieval_hash_key,
            hash_key_id=settings.retrieval_hash_key_id,
            embedder=embedder,
            rrf_k=settings.rrf_k,
            retrieval_mode=retrieval_mode or "lexical",
            max_tokens=settings.llm_max_tokens,
            temperature=settings.llm_temperature,
        )
        outcome = service.answer(
            query,
            knowledge_scope=knowledge_scope,
            language=language,
            max_evidence=max_evidence,
            context=SearchContext(
                tenant_id=context.tenant_id,
                principal_id=context.principal_id,
                subject=context.to_subject(),
                allowed_scopes=context.allowed_scopes,
                trace_id=trace_id,
            ),
        )
    except SearchScopeNotAuthorized as exc:
        raise ApiError(ErrorCode.ACCESS_DENIED, detail=f"knowledge_scope 未授权: {exc}") from exc
    except _UNAVAILABLE as exc:
        raise ApiError(ErrorCode.SERVICE_UNAVAILABLE, detail=str(exc)) from exc

    return dict(outcome.payload)


def resolve_retrieval(settings: Settings) -> tuple[str, Any]:
    """解析召回模式与 embedding；与 `/v1/knowledge/answer` 同口径。"""
    from fkh.api.routes_search import resolve_embedder

    try:
        return resolve_embedder(settings)
    except EmbeddingNotConfigured as exc:
        raise ApiError(ErrorCode.SERVICE_UNAVAILABLE, detail=str(exc)) from exc
