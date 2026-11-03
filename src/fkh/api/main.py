"""knowledge-api 应用装配。

Phase 0 的定位：把所有契约端点注册出来、把认证与错误格式固定下来，
业务端点返回 501 而不是伪造结果。这样契约测试、网关接入和前端联调
可以在检索链路完成前先跑起来，同时不会让任何未实现能力看起来"能用"。
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import psycopg
from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse

from fkh.api.errors import ApiError, ErrorCode, install_error_handlers
from fkh.api.models import AnswerRequest, TrainingAskRequest
from fkh.api.security import PrincipalContext, PrincipalDep, require
from fkh.config import Settings, get_settings

# (方法, 路径, 所需权限, 说明)
CONTRACT_ROUTES: list[tuple[str, str, str, str]] = [
    ("POST", "/v1/sources", "knowledge.ingest", "申请上传与导入"),
    ("POST", "/v1/interviews", "interview.create", "建立语音访谈任务"),
    ("GET", "/v1/interviews/{interview_id}", "interview.read", "查询转写与草稿状态"),
    ("POST", "/v1/knowledge/drafts", "knowledge.write", "创建知识草稿"),
    ("POST", "/v1/knowledge/{knowledge_id}/review", "knowledge.review", "技术审核"),
    ("POST", "/v1/knowledge/{knowledge_id}/publish", "knowledge.publish", "内部发布"),
    ("POST", "/v1/knowledge/{knowledge_id}/revoke", "knowledge.revoke", "撤销知识与发布"),
    ("POST", "/v1/knowledge/search", "knowledge.search", "权限过滤检索"),
    ("GET", "/v1/evidence/{evidence_id}", "evidence.read", "受控证据定位"),
    ("POST", "/v1/feedback", "feedback.create", "反馈与坏例登记"),
    ("POST", "/v1/training/courses", "training_admin", "创建课程草稿"),
    ("POST", "/v1/training/courses/{course_id}/publish", "training_publisher", "发布课程"),
    ("POST", "/v1/training/assignments", "training_admin", "按岗位分配课程"),
    ("GET", "/v1/training/me/assignments", "learner", "查询本人学习任务"),
    ("POST", "/v1/training/attempts", "learner", "提交测验"),
    ("POST", "/v1/training/practical-signoffs", "mentor", "导师实操签核"),
    ("POST", "/v1/training/feedback", "learner", "培训反馈"),
    ("GET", "/v1/training/assignments/{assignment_id}/progress", "learner", "学习进度"),
]


def _not_implemented(label: str) -> Callable[[Request], Awaitable[None]]:
    async def _handler(request: Request) -> None:
        raise ApiError(ErrorCode.NOT_IMPLEMENTED, detail=f"{label} 属于后续阶段交付")

    return _handler


def _register_contract_routes(app: FastAPI) -> None:
    for method, path, scope, summary in CONTRACT_ROUTES:
        app.add_api_route(
            path,
            _not_implemented(summary),
            methods=[method],
            summary=summary,
            dependencies=[Depends(require(scope))],
            responses={
                401: {"description": "未认证"},
                403: {"description": "无权限"},
                501: {"description": "当前阶段未实现"},
            },
            tags=["knowledge"] if path.startswith("/v1/knowledge") or "/interviews" in path
            else (["evidence"] if "/evidence" in path else (["training"] if "/training" in path else ["ops"])),
        )


async def _answer(request: Request, payload: AnswerRequest, context: PrincipalDep) -> JSONResponse:
    """带引用问答入口。

    入参校验与权限收窄在此完成，检索与生成留到 Phase 2。
    先校验再拒绝，可确保契约层（请求模型、scope 收窄、错误格式）现在就被真实验证。
    """
    context.narrow_knowledge_scope(payload.knowledge_scope)
    raise ApiError(ErrorCode.NOT_IMPLEMENTED, detail="检索与生成链路尚未接入（Phase 2）")


async def _training_ask(request: Request, payload: TrainingAskRequest, context: PrincipalDep) -> JSONResponse:
    context.require_scope("learner")
    raise ApiError(ErrorCode.NOT_IMPLEMENTED, detail="培训问答尚未接入（Phase 3）")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(
        title="Factory Knowledge Hub · knowledge-api",
        version="0.1.0",
        description=(
            "受控知识治理与可引用 RAG。Phase 0 仅提供健康探针与契约端点，"
            "业务端点返回 501；契约以 contracts/openapi/openapi.yaml 为单一来源。"
        ),
    )

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
        return JSONResponse(status_code=200, content={"status": "ok", "checks": {"database": "ok"}})

    _register_contract_routes(app)

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
            501: {"description": "检索链路未接入"},
        },
    )
    app.add_api_route(
        "/v1/training/ask",
        _training_ask,
        methods=["POST"],
        summary="培训带引用问答",
        tags=["training"],
        dependencies=[Depends(require("learner"))],
        responses={
            401: {"description": "未认证"},
            403: {"description": "无权限"},
            501: {"description": "培训问答未接入"},
        },
    )
    return app


app = create_app()


__all__ = ["app", "create_app", "PrincipalContext"]
