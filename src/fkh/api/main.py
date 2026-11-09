"""knowledge-api 应用装配。

V1 只暴露已实现的契约端点；未进入 V1 的 `/v1/sources` 与
`/v1/knowledge/drafts` 已从公开契约和路由中移除，避免出现“契约声明了但永远 501”
的半成品。现有导入入口是 `POST /v1/knowledge/documents`，草稿由上传/审核流程产生。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Annotated, Any

import psycopg
from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse

from fkh.answer import AnswerNotConfigured, AnswerService, build_commitment_keywords
from fkh.api.db import CommitBeforeSend, create_pool, tenant_connection
from fkh.api.errors import ApiError, ErrorCode, error_body, install_error_handlers
from fkh.api.models import AnswerRequest
from fkh.api.routes_catalog_conflicts import router as catalog_conflicts_router
from fkh.api.routes_config import router as config_router
from fkh.api.routes_context import router as context_router
from fkh.api.routes_documents import router as documents_router
from fkh.api.routes_evidence import router as evidence_router
from fkh.api.routes_feedback import router as feedback_router
from fkh.api.routes_interview import router as interview_router
from fkh.api.routes_review import router as review_router
from fkh.api.routes_search import resolve_embedder
from fkh.api.routes_search import router as search_router
from fkh.api.routes_training import router as training_router
from fkh.api.security import PrincipalContext, PrincipalDep, require
from fkh.config import Settings, get_settings
from fkh.generation import GenerationError, GenerationNotConfigured, get_generator
from fkh.retrieval.embedding import EmbeddingNotConfigured
from fkh.retrieval.search import (
    HYBRID_MODE,
    SCORING_BY_MODE,
    SearchContext,
    SearchNotConfigured,
    SearchScopeNotAuthorized,
    SearchUnavailable,
)


def _answer(
    request: Request,
    payload: AnswerRequest,
    context: PrincipalDep,
    settings: Annotated[Settings, Depends(get_settings)],
) -> JSONResponse:
    """带引用问答。

    顺序是刻意的（L1 裁决 §第 1 项）：**鉴权 → 请求校验 → 能力配置 → 检索 → 生成**。
    越权不能被"配置没配好"掩盖成 503，所以能力检查排在 scope 收窄之后。

    失败口径：
    - 403：scope 越权（含上游 SearchScopeNotAuthorized）；
    - 503：推理能力未就绪/不可达（`ERROR` 审计，不伪装成 REFUSED）；
    - 200：ANSWERED / REFUSED / HUMAN_REVIEW_REQUIRED 三种业务状态。
    """
    context.narrow_knowledge_scope(payload.knowledge_scope)
    trace_id = str(getattr(request.state, "trace_id", uuid.uuid4().hex))

    try:
        mode, embedder = resolve_embedder(settings)
    except EmbeddingNotConfigured as exc:
        raise ApiError(ErrorCode.SERVICE_UNAVAILABLE, detail=str(exc)) from exc

    try:
        generator = get_generator(
            settings.llm_provider,
            base_url=settings.llm_base_url,
            model=settings.llm_model,
            timeout_seconds=settings.llm_timeout_seconds,
        )
    except (GenerationNotConfigured, GenerationError) as exc:
        # 非 loopback / 模型不存在 / provider 非法 / 本地推理不可达 都在这里失败。
        # 构造期探测失败抛的是 GenerationError，若不捕获会变成 500——
        # 而 SPEC 明确"能力故障是 503 + ERROR 审计"，不能伪装成服务端 bug。
        raise ApiError(ErrorCode.SERVICE_UNAVAILABLE, detail=str(exc)) from exc

    # 数据库连接按需获取（而不是做成依赖）：依赖会在调用端点前解析，
    # 数据库没配置的 503 会抢先盖住 403/422，把既有契约断言打乱。
    pool = getattr(request.app.state, "pool", None)
    if pool is None:
        raise ApiError(ErrorCode.SERVICE_UNAVAILABLE, detail="数据库未配置，业务端点不可用")

    try:
        with tenant_connection(pool, context.tenant_id, isolation="REPEATABLE READ") as conn:
            service = AnswerService(
                conn,
                generator=generator,
                hash_key=settings.retrieval_hash_key,
                hash_key_id=settings.retrieval_hash_key_id,
                embedder=embedder,
                rrf_k=settings.rrf_k,
                retrieval_mode=mode,
                max_tokens=settings.llm_max_tokens,
                temperature=settings.llm_temperature,
                commitment_keywords=build_commitment_keywords(
                    settings.commitment_keywords_extra
                ),
            )
            outcome = service.answer(
                payload.query,
                knowledge_scope=payload.knowledge_scope,
                language=payload.language,
                max_evidence=payload.max_evidence,
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
    except (SearchNotConfigured, SearchUnavailable, AnswerNotConfigured, GenerationError) as exc:
        raise ApiError(ErrorCode.SERVICE_UNAVAILABLE, detail=str(exc)) from exc

    return JSONResponse(content=outcome.payload)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # 数据库未配置时不建池，业务端点统一返回 503；
        # 配置了却连不上则直接启动失败，不允许"看起来健康但什么都不通"。
        pool = create_pool(settings.database_url) if settings.database_url else None
        app.state.pool = pool
        try:
            yield
        finally:
            if pool is not None:
                pool.close()

    app = FastAPI(
        title="Factory Knowledge Hub · knowledge-api",
        version="0.1.0",
        description=(
            "受控知识治理与可引用 RAG。审核/发布/撤销已可用；检索与问答在 Phase 2 接入。"
            "契约以 contracts/openapi/openapi.yaml 为单一来源。"
        ),
        lifespan=lifespan,
    )
    # 先注册：后续 BaseHTTPMiddleware 会插到它前面，使本中间件紧贴路由，
    # 能在依赖退出（事务提交）之前看到 response.start 并完成提交。
    app.add_middleware(CommitBeforeSend)

    @app.middleware("http")
    async def trace_middleware(
        request: Request, call_next: Callable[[Request], Awaitable[Any]]
    ) -> Any:
        trace_id = request.headers.get("X-Trace-Id") or uuid.uuid4().hex
        request.state.trace_id = trace_id
        response = await call_next(request)
        response.headers["X-Trace-Id"] = trace_id
        return response

    install_error_handlers(app)

    # 让依赖注入拿到的是本实例的配置，而不是进程级缓存的那一份。
    # 缺这一步会使 create_app(custom_settings) 在测试与多实例部署中失效：
    # 端点用 A 配置启动，鉴权依赖却读 B 配置（通常是空的），所有请求变成 401。
    app.dependency_overrides[get_settings] = lambda: settings

    @app.get("/health/live", tags=["health"], summary="存活探针")
    def live() -> dict[str, str]:
        return {"status": "ok", "service": settings.service_name, "version": "0.1.0"}

    @app.get("/health/ready", tags=["health"], summary="依赖就绪探针")
    def ready() -> JSONResponse:
        if not settings.database_url:
            # 未配置数据库时不得报告 ready，否则编排层会向不可用实例导流
            return JSONResponse(
                status_code=503,
                content={"status": "degraded", "checks": {"database": "not_configured"}},
            )
        try:
            with psycopg.connect(settings.database_url, connect_timeout=2) as conn:
                conn.execute("SELECT 1")
        except psycopg.Error:
            return JSONResponse(
                status_code=503,
                content={"status": "down", "checks": {"database": "unreachable"}},
            )

        # 混合检索是服务端显式 opt-in 的能力：配了就必须真的可用。
        # 模型不可用时宁可 503 让编排层不导流，也不要在请求期才发现（判决 Q2）。
        mode = settings.retrieval_mode.strip().lower()
        if mode not in SCORING_BY_MODE:
            return JSONResponse(
                status_code=503,
                content={
                    "status": "degraded",
                    "checks": {"database": "ok", "retrieval": "invalid_mode"},
                },
            )
        if mode == HYBRID_MODE:
            try:
                resolve_embedder(settings)
            except (EmbeddingNotConfigured, SearchNotConfigured) as exc:
                return JSONResponse(
                    status_code=503,
                    content={
                        "status": "degraded",
                        "checks": {"database": "ok", "retrieval": "embedding_unavailable"},
                        "detail": str(exc),
                    },
                )
            return JSONResponse(
                status_code=200,
                content={"status": "ok", "checks": {"database": "ok", "retrieval": "ok"}},
            )

        return JSONResponse(status_code=200, content={"status": "ok", "checks": {"database": "ok"}})


    MAX_REQUEST_BYTES = 8 * 1024 * 1024
    MAX_FEEDBACK_BYTES = 16 * 1024
    _BODY_LIMITS = {
        "/v1/knowledge/documents": MAX_REQUEST_BYTES,
        "/v1/feedback": MAX_FEEDBACK_BYTES,
        "/v1/training/feedback": MAX_FEEDBACK_BYTES,
    }

    def _payload_too_large(request: Request) -> JSONResponse:
        return JSONResponse(
            status_code=413,
            content=error_body(
                ErrorCode.PAYLOAD_TOO_LARGE, str(getattr(request.state, "trace_id", "-"))
            ),
        )

    @app.middleware("http")
    async def _limit_upload_body(
        request: Request, call_next: Callable[[Request], Awaitable[Any]]
    ) -> Any:
        # 请求体硬上限（L1 终审 2026-11-07 §3）：只查 Content-Length 不够，
        # chunked 或"声明长度与实际不符"都能绕过，因此在接收层逐块累计。
        # 反馈体只需要 trace_id/category/两个标量，单独收紧到 16 KiB；
        # 超过上限立刻 413，不把整包缓冲完再判断。
        limit = _BODY_LIMITS.get(request.url.path) if request.method == "POST" else None
        if limit is not None:
            declared = request.headers.get("content-length")
            if declared is not None and declared.isdigit() and int(declared) > limit:
                return _payload_too_large(request)
            received = 0
            buffered: list[bytes] = []
            async for chunk in request.stream():
                received += len(chunk)
                if received > limit:
                    return _payload_too_large(request)
                buffered.append(chunk)
            # 回填已缓冲正文供下游解析；否则流已耗尽，解析会拿到空 body
            request._body = b"".join(buffered)
        return await call_next(request)

    app.include_router(catalog_conflicts_router)
    app.include_router(documents_router)
    app.include_router(documents_router)
    app.include_router(review_router)
    app.include_router(context_router)
    app.include_router(training_router)
    app.include_router(interview_router)
    app.include_router(config_router)
    app.include_router(evidence_router)
    app.include_router(feedback_router)
    app.include_router(search_router)

    app.add_api_route(
        "/v1/knowledge/answer",
        _answer,
        methods=["POST"],
        summary="带引用问答",
        tags=["knowledge"],
        dependencies=[Depends(require("knowledge.answer"))],
        responses={
            200: {"description": "见 contracts/schema/answer_response.json"},
            401: {"description": "未认证"},
            403: {"description": "无权限或 scope 越界"},
            503: {"description": "本地推理能力未就绪或不可达（审计记 ERROR）"},
        },
    )
    return app


app = create_app()


__all__ = ["app", "create_app", "PrincipalContext"]
