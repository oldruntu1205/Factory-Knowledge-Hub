"""检索端点（`POST /v1/knowledge/search`）。

召回模式由**服务端配置**决定（`FKH_RETRIEVAL_MODE`，默认 `lexical`），不是请求参数：
调用方只能观察响应里的 `scoring`，不能自己选择算法（2026-11-04 L1 判决 Q2）。

启用 `hybrid` 后：模型未安装 / 权重缺失 / 推理失败一律返回 503，不静默降级成词法结果。
`/v1/knowledge/context` 不受此开关影响，仍固定为词法（判决 Q7）。

权限分两层：
- 接口权限 `knowledge.search` 由依赖注入校验；
- 数据范围权限：令牌 `allowed_scopes` 必须包含请求的 `knowledge_scope`，
  否则 403（不能伪装成"没有结果"）。

响应只给定位与片段：不给全文（走证据端点）、不给 score、不给候选数或过滤统计。
"""

from __future__ import annotations

from typing import Annotated, Any
from uuid import uuid4

import psycopg
from fastapi import APIRouter, Depends, Request

from fkh.api.db import get_conn
from fkh.api.errors import ApiError, ErrorCode
from fkh.api.models import SearchRequest
from fkh.api.security import PrincipalDep
from fkh.config import Settings, get_settings
from fkh.retrieval.embedding import Embedder, EmbeddingNotConfigured, get_embedder
from fkh.retrieval.search import (
    HYBRID_MODE,
    SearchContext,
    SearchNotConfigured,
    SearchScopeNotAuthorized,
    SearchService,
    SearchUnavailable,
)

router = APIRouter(tags=["knowledge"])

ConnDep = Annotated[psycopg.Connection[Any], Depends(get_conn)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


def resolve_embedder(settings: Settings) -> tuple[str, Embedder | None]:
    """把配置解析成（实际模式, embedder）。

    只有显式 `hybrid` 才会尝试加载模型：纯词法不得因为装了 embedding 依赖就付出加载成本。
    模型不可用时抛 `EmbeddingNotConfigured`，由调用方映射成 503。
    """
    mode = settings.retrieval_mode.strip().lower()
    if mode != HYBRID_MODE:
        return mode, None
    embedder = get_embedder(
        settings.embedding_provider,
        device=settings.embedding_device,
        batch_size=settings.embedding_batch_size,
        env=settings.env,
    )
    return mode, embedder


@router.post("/v1/knowledge/search", summary="权限过滤检索")
def search(
    payload: SearchRequest,
    request: Request,
    context: PrincipalDep,
    conn: ConnDep,
    settings: SettingsDep,
) -> dict[str, Any]:
    context.require_scope("knowledge.search")
    trace_id = str(getattr(request.state, "trace_id", uuid4().hex))

    try:
        mode, embedder = resolve_embedder(settings)
    except EmbeddingNotConfigured as exc:
        # 配置了混合检索但模型不可用：可观测的 503，不返回空结果冒充"没搜到"
        raise ApiError(ErrorCode.SERVICE_UNAVAILABLE, detail=str(exc)) from exc

    service = SearchService(
        conn,
        hash_key=settings.retrieval_hash_key,
        hash_key_id=settings.retrieval_hash_key_id,
        embedder=embedder,
        rrf_k=settings.rrf_k,
    )
    try:
        result = service.search(
            payload.query,
            knowledge_scope=payload.knowledge_scope,
            max_results=payload.max_results,
            context=SearchContext(
                tenant_id=context.tenant_id,
                principal_id=context.principal_id,
                subject=context.to_subject(),
                allowed_scopes=context.allowed_scopes,
                trace_id=trace_id,
            ),
            retrieval_mode=mode,
        )
    except SearchScopeNotAuthorized as exc:
        raise ApiError(ErrorCode.ACCESS_DENIED, detail=f"knowledge_scope 未授权: {exc}") from exc
    except (SearchNotConfigured, SearchUnavailable) as exc:
        raise ApiError(ErrorCode.SERVICE_UNAVAILABLE, detail=str(exc)) from exc

    return {
        "results": [
            {
                "chunk_id": str(hit.chunk_id),
                "version_id": str(hit.version_id),
                "knowledge_id": str(hit.document_id),
                "text_snippet": hit.text_snippet,
                "snippet_truncated": hit.snippet_truncated,
                "locator": hit.locator,
                "heading_path": list(hit.heading_path) if hit.heading_path is not None else None,
            }
            for hit in result.hits
        ],
        "scoring": result.scoring,
        "trace_id": trace_id,
    }


__all__ = ["resolve_embedder", "router"]
