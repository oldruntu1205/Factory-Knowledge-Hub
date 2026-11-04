"""上下文端点（`POST /v1/knowledge/context`）。

定位：问答链路的前置步骤，把「检索命中」转成「可交给生成的上下文块」。
它不是对外承诺的接口，供前端预览与调试使用。

两条不能绕过的约束：
1. 必须走 `SearchService` —— 检索必须留审计（谁在什么范围查了多久），
   直接调 `LexicalRetriever` 会绕过 `retrieval_queries`；
2. 权限与范围校验与检索端点一致，上下文里的每一块都经过 ACL 判定，
   父块只有在「全部子块可见」时才会整段返回。
"""

from __future__ import annotations

from typing import Annotated, Any
from uuid import uuid4

import psycopg
from fastapi import APIRouter, Depends, Request

from fkh.api.db import get_conn
from fkh.api.errors import ApiError, ErrorCode
from fkh.api.models import ContextRequest
from fkh.api.security import PrincipalDep
from fkh.config import Settings, get_settings
from fkh.retrieval.context import assemble_context
from fkh.retrieval.search import (
    LEXICAL_MODE,
    SearchContext,
    SearchNotConfigured,
    SearchScopeNotAuthorized,
    SearchService,
)

router = APIRouter(tags=["knowledge"])

ConnDep = Annotated[psycopg.Connection[Any], Depends(get_conn)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


@router.post("/v1/knowledge/context", summary="检索并组装生成用上下文")
def context(
    payload: ContextRequest,
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
            # 判决 Q7：本切片只授权 search 启用混合；context 的候选来源会改变拒答分档，
            # 在真实模型与证据质量未验证前固定为词法，接混合须另行送审并补分档回归。
            retrieval_mode=LEXICAL_MODE,
        )
    except SearchScopeNotAuthorized as exc:
        raise ApiError(ErrorCode.ACCESS_DENIED, detail=f"knowledge_scope 未授权: {exc}") from exc
    except SearchNotConfigured as exc:
        raise ApiError(ErrorCode.SERVICE_UNAVAILABLE, detail=str(exc)) from exc

    assembly = assemble_context(
        conn,
        context.tenant_id,
        result.hits,
        context.to_subject(),
        max_blocks=payload.max_blocks,
        max_tokens=payload.max_tokens,
    )

    return {
        "blocks": [
            {
                "chunk_id": str(block.chunk_id),
                "version_id": str(block.version_id),
                "knowledge_id": str(block.document_id),
                "from_parent": block.from_parent,
                "text": block.text,
                "tokens": block.tokens,
                "locator": block.locator,
                "heading_path": list(block.heading_path) if block.heading_path else None,
            }
            for block in assembly.blocks
        ],
        "stats": {
            "denied": assembly.denied,
            "dropped": assembly.dropped,
            "parent_restores": assembly.parent_restores,
            "tokens": assembly.tokens,
        },
        "trace_id": trace_id,
    }


__all__ = ["router"]
