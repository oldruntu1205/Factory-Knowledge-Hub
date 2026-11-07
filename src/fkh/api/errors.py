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
    NOT_FOUND = "NOT_FOUND"
    PAYLOAD_TOO_LARGE = "PAYLOAD_TOO_LARGE"
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
    ErrorCode.NOT_FOUND: (404, "Resource not found"),
    ErrorCode.PAYLOAD_TOO_LARGE: (413, "Payload too large"),
    ErrorCode.NOT_IMPLEMENTED: (501, "Endpoint is not implemented in this phase"),
    ErrorCode.VERSION_CONFLICT: (409, "Resource version conflict"),
    ErrorCode.RATE_LIMITED: (429, "Too many requests"),
    ErrorCode.SERVICE_UNAVAILABLE: (503, "Service dependency unavailable"),
    ErrorCode.INTERNAL_ERROR: (500, "Internal error"),
}


class ApiError(Exception):
    def __init__(
        self,
        code: ErrorCode,
        *,
        detail: str | None = None,
        hint: str | None = None,
    ) -> None:
        """hint 是**可以给调用方看**的简短说明（字段名 + 规则），不是 detail。

        边界（与 L1 §6.3 一致）：
        - 只允许出现在 422/413 这类"请求本身不合法"的响应里；
        - 一律不得包含用户提交的原始值、内部标识、路径或模型输出；
        - 401/403/404 保持固定文案，避免用错误信息探测资源存在性。
        """
        super().__init__(code)
        self.code = code
        self.status_code, self.public_message = _ERROR_TABLE[code]
        # detail 只进日志，不进响应体
        self.detail = detail
        self.hint = hint


_HINTABLE = {ErrorCode.VALIDATION_ERROR, ErrorCode.PAYLOAD_TOO_LARGE}
MAX_HINT_CHARS = 200

# FastAPI 校验错误类型 → 对外规则文案（固定词表：不把 input/ctx 拼进来）
_RULES = {
    "missing": "required",
    "extra_forbidden": "not allowed",
    "string_too_long": "too long",
    "string_too_short": "too short",
    "string_type": "must be a string",
    "int_parsing": "must be an integer",
    "int_type": "must be an integer",
    "float_parsing": "must be a number",
    "bool_parsing": "must be a boolean",
    "enum": "not one of the allowed values",
    "literal_error": "not one of the allowed values",
    "json_invalid": "must be valid JSON",
    "dict_type": "must be an object",
    "list_type": "must be an array",
    "greater_than_equal": "below the allowed minimum",
    "less_than_equal": "above the allowed maximum",
}


_SCOPES = {"body", "query", "path", "header", "cookie"}


def _loc(errors: list[dict[str, object]], index: int) -> str:
    raw = errors[index].get("loc")
    parts_raw: list[object] = list(raw) if isinstance(raw, (list, tuple)) else []
    scope = str(parts_raw[0]) if parts_raw and str(parts_raw[0]) in _SCOPES else "request"
    fields = [str(part) for part in parts_raw if str(part) not in _SCOPES]
    return f"{scope}.{'.'.join(fields)}" if fields else scope


def validation_hint(errors: list[dict[str, object]]) -> str:
    """把 FastAPI 的校验错误收敛成"字段 + 规则"的简短说明。

    刻意**不**包含 `input`（可能是正文或用户 query）与 `ctx`（可能回显提交值）：
    422 里给字段名足够定位，给值只会把敏感输入写进日志与 APM。
    """
    parts: list[str] = []
    for index in range(min(len(errors), 3)):
        rule = _RULES.get(str(errors[index].get("type")), "invalid")
        parts.append(f"{_loc(errors, index)}: {rule}")
    if len(errors) > 3:
        parts.append(f"…(+{len(errors) - 3} more)")
    hint = "; ".join(parts)
    return hint[:MAX_HINT_CHARS]


def error_body(
    code: ErrorCode, trace_id: str, hint: str | None = None
) -> dict[str, dict[str, str]]:
    _, message = _ERROR_TABLE[code]
    if hint and code in _HINTABLE:
        message = f"{message} ({hint[:MAX_HINT_CHARS]})"
    return {"error": {"code": code.value, "message": message, "trace_id": trace_id}}


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _handle_api_error(request: Request, exc: ApiError) -> JSONResponse:
        trace_id = getattr(request.state, "trace_id", "-")
        return JSONResponse(
            status_code=exc.status_code, content=error_body(exc.code, trace_id, exc.hint)
        )

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
        hint = validation_hint([dict(item) for item in exc.errors()])
        return JSONResponse(
            status_code=422,
            content=error_body(ErrorCode.VALIDATION_ERROR, trace_id, hint),
        )

    @app.exception_handler(Exception)
    async def _handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        trace_id = getattr(request.state, "trace_id", "-")
        return JSONResponse(status_code=500, content=error_body(ErrorCode.INTERNAL_ERROR, trace_id))
