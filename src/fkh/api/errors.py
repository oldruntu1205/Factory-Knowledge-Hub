"""统一错误响应（L1 §6.3）。

约束：
- 403 不区分"文档不存在"与"无权限"，避免用错误码探测资源存在性；
- message 为固定文案，不携带内部标识、路径或模型原始输出；
- 所有响应都带 trace_id，便于审计关联。
"""

from __future__ import annotations

from enum import StrEnum

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


class ErrorCode(StrEnum):
    UNAUTHENTICATED = "UNAUTHENTICATED"
    ACCESS_DENIED = "ACCESS_DENIED"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    NOT_IMPLEMENTED = "NOT_IMPLEMENTED"
    VERSION_CONFLICT = "VERSION_CONFLICT"
    RATE_LIMITED = "RATE_LIMITED"
    SERVICE_UNAVAILABLE = "SERVICE_UNAVAILABLE"
    INTERNAL_ERROR = "INTERNAL_ERROR"


# code -> (HTTP 状态码, 对外文案)
_ERROR_TABLE: dict[ErrorCode, tuple[int, str]] = {
    ErrorCode.UNAUTHENTICATED: (401, "Authentication required"),
    ErrorCode.ACCESS_DENIED: (403, "Request cannot be fulfilled"),
    ErrorCode.VALIDATION_ERROR: (422, "Request payload is invalid"),
    ErrorCode.NOT_IMPLEMENTED: (501, "Endpoint is not implemented in this phase"),
    ErrorCode.VERSION_CONFLICT: (409, "Resource version conflict"),
    ErrorCode.RATE_LIMITED: (429, "Too many requests"),
    ErrorCode.SERVICE_UNAVAILABLE: (503, "Service dependency unavailable"),
    ErrorCode.INTERNAL_ERROR: (500, "Internal error"),
}


class ApiError(Exception):
    def __init__(self, code: ErrorCode, *, detail: str | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.status_code, self.public_message = _ERROR_TABLE[code]
        # detail 只进日志，不进响应体
        self.detail = detail


def error_body(code: ErrorCode, trace_id: str) -> dict[str, dict[str, str]]:
    _, message = _ERROR_TABLE[code]
    return {"error": {"code": code.value, "message": message, "trace_id": trace_id}}


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _handle_api_error(request: Request, exc: ApiError) -> JSONResponse:
        trace_id = getattr(request.state, "trace_id", "-")
        return JSONResponse(status_code=exc.status_code, content=error_body(exc.code, trace_id))

    @app.exception_handler(RequestValidationError)
    async def _handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        """请求体不合法时**不得回显请求内容**。

        FastAPI 默认把 `input` 与 `ctx` 一起放进 422 响应，检索场景下就等于把用户的
        原始 query 写回了响应体（还会被前端日志、APM 采集）。这里统一收敛成固定文案，
        细节只留在服务端（如需排查，用 trace_id 关联，不落原始值）。
        """
        trace_id = getattr(request.state, "trace_id", "-")
        return JSONResponse(
            status_code=422, content=error_body(ErrorCode.VALIDATION_ERROR, trace_id)
        )

    @app.exception_handler(Exception)
    async def _handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        trace_id = getattr(request.state, "trace_id", "-")
        return JSONResponse(status_code=500, content=error_body(ErrorCode.INTERNAL_ERROR, trace_id))
