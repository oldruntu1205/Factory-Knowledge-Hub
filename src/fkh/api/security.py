"""身份与服务端授权上下文（L1 §6.1）。

关键约束：
- tenant_id / principal_id 一律取自校验通过的令牌，绝不接受请求体或查询参数覆写；
- 客户端传入的 knowledge_scope 只能缩小服务端已授予的集合；
- 未配置签名密钥时拒绝一切认证请求，而不是放开（ADR-02 签署前保持 fail-closed）。
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Annotated, Any
from uuid import UUID

import jwt
from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from fkh.api.errors import ApiError, ErrorCode
from fkh.authz.policy import Subject
from fkh.config import Settings, get_settings

_bearer = HTTPBearer(auto_error=False)


@dataclass(frozen=True, slots=True)
class PrincipalContext:
    tenant_id: UUID
    principal_id: UUID
    subject: str
    roles: frozenset[str]
    scopes: frozenset[str]
    allowed_scopes: frozenset[str]
    # ACL 的四个维度里，roles/groups/departments/subjects 都会参与判定。
    # 这里如果漏解析 groups/departments，策略里按部门授权的规则在真实请求下会
    # 静默失效——不报错、只是永远不匹配，比报错更难发现。
    groups: frozenset[str] = frozenset()
    departments: frozenset[str] = frozenset()

    def require_scope(self, scope: str) -> None:
        if scope not in self.scopes:
            raise ApiError(ErrorCode.ACCESS_DENIED, detail=f"缺少权限 {scope}")

    def narrow_knowledge_scope(self, requested: str) -> str:
        """调用方只能缩小范围；越界即拒绝，而不是静默降级。"""
        if requested not in self.allowed_scopes:
            raise ApiError(ErrorCode.ACCESS_DENIED, detail=f"knowledge_scope={requested} 超出授权范围")
        return requested

    def to_subject(self) -> Subject:
        """转成 ACL 判定用的主体。两条链路的字段必须一一对应，否则 ACL 形同虚设。"""
        return Subject(
            principal_id=str(self.principal_id),
            tenant_id=str(self.tenant_id),
            roles=self.roles,
            groups=self.groups,
            departments=self.departments,
        )


def _as_uuid(value: Any, claim: str) -> UUID:
    try:
        return UUID(str(value))
    except (TypeError, ValueError) as exc:
        raise ApiError(ErrorCode.UNAUTHENTICATED, detail=f"claim {claim} 非法") from exc


def _as_str_set(value: Any) -> frozenset[str]:
    if value is None:
        return frozenset()
    if isinstance(value, str):
        return frozenset(part for part in value.replace(",", " ").split() if part)
    if isinstance(value, list | tuple | set):
        return frozenset(str(item) for item in value)
    raise ApiError(ErrorCode.UNAUTHENTICATED, detail="claim 类型非法")


def decode_token(token: str, settings: Settings) -> PrincipalContext:
    if not settings.jwt_secret:
        # ADR-02 未签署：不提供任何可用身份源
        raise ApiError(ErrorCode.UNAUTHENTICATED, detail="身份源未配置")

    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
            audience=settings.jwt_audience,
            issuer=settings.jwt_issuer,
            options={"require": ["exp", "iss", "aud", "sub"]},
        )
    except jwt.InvalidTokenError as exc:
        raise ApiError(ErrorCode.UNAUTHENTICATED, detail=str(exc)) from exc

    return PrincipalContext(
        tenant_id=_as_uuid(payload.get("tenant_id"), "tenant_id"),
        principal_id=_as_uuid(payload.get("principal_id"), "principal_id"),
        subject=str(payload["sub"]),
        roles=_as_str_set(payload.get("roles")),
        scopes=_as_str_set(payload.get("scope")),
        allowed_scopes=_as_str_set(payload.get("allowed_scopes")),
        groups=_as_str_set(payload.get("groups")),
        departments=_as_str_set(payload.get("departments")),
    )


async def current_principal(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> PrincipalContext:
    if credentials is None or not credentials.credentials:
        raise ApiError(ErrorCode.UNAUTHENTICATED, detail="缺少 Bearer 令牌")
    context = decode_token(credentials.credentials, settings)
    # 供后续依赖与审计使用；不写入响应
    request.state.tenant_id = str(context.tenant_id)
    request.state.principal_id = str(context.principal_id)
    return context


PrincipalDep = Annotated[PrincipalContext, Depends(current_principal)]


def require(scope: str) -> Callable[[PrincipalContext], Coroutine[Any, Any, PrincipalContext]]:
    """生成带权限校验的依赖。"""

    async def _dep(context: PrincipalDep) -> PrincipalContext:
        context.require_scope(scope)
        return context

    return _dep
