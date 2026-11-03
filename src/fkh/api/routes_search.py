"""词法检索端点（`POST /v1/knowledge/search`）。

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
from fkh.retrieval.lexical import SCORING
from fkh.retrieval.search import (
    SearchContext,
    SearchNotConfigured,
    SearchScopeNotAuthorized,
    SearchService,
)

router = APIRouter(tags=["knowledge"])

ConnDep = Annotated[psycopg.Connection[Any], Depends(get_conn)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


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

    service = SearchService(
        conn,
        hash_key=settings.retrieval_hash_key,
        hash_key_id=settings.retrieval_hash_key_id,
    )
    try:
        hits = service.search(
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
        )
    except SearchScopeNotAuthorized as exc:
        raise ApiError(ErrorCode.ACCESS_DENIED, detail=f"knowledge_scope 未授权: {exc}") from exc
    except SearchNotConfigured as exc:
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
            for hit in hits
        ],
        "scoring": SCORING,
        "trace_id": trace_id,
    }


__all__ = ["router"]
