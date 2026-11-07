"""审核 / 发布 / 撤销端点（L1 §6.2）。

权限分层：
- review 需要 knowledge.review
- publish 需要 knowledge.publish；对外公开额外要求 knowledge.publish_public
- revoke 需要 knowledge.revoke

公开审批的主体隔离由 ReviewService 强制（public_approver ≠ approved_by），
不依赖调用方自觉。
"""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

import psycopg
from fastapi import APIRouter, Depends

from fkh.api.db import get_conn
from fkh.api.errors import ApiError, ErrorCode
from fkh.api.models import PublishRequest, ReviewRequest
from fkh.api.security import PrincipalDep
from fkh.domain.review import ReviewConflict, ReviewService, VersionNotFound, VersionSnapshot
from fkh.domain.state_machine import IllegalTransition, VersionStatus

router = APIRouter(tags=["knowledge"])

ConnDep = Annotated[psycopg.Connection[Any], Depends(get_conn)]


def _parse_uuid(raw: str, field: str) -> UUID:
    try:
        return UUID(raw)
    except ValueError as exc:
        raise ApiError(
            ErrorCode.VALIDATION_ERROR,
            detail=f"{field} 不是合法 UUID",
            hint=f"{field}: must be a UUID",
        ) from exc


def _view(snapshot: VersionSnapshot) -> dict[str, Any]:
    return {
        "version_id": str(snapshot.id),
        "status": snapshot.status.value,
        "version_no": snapshot.version_no,
        "classification": snapshot.classification,
        "audience": snapshot.audience,
        "approved_by": str(snapshot.approved_by) if snapshot.approved_by else None,
        "public_approved_by": (
            str(snapshot.public_approved_by) if snapshot.public_approved_by else None
        ),
    }


def _translate(exc: Exception) -> ApiError:
    """把领域异常翻译成对外错误。

    VersionNotFound 一律返回 403 而不是 404：跨租户访问与"版本真的不存在"
    必须不可区分，否则错误码会被用来探测资源（L1 §6.1）。
    """
    if isinstance(exc, VersionNotFound):
        return ApiError(ErrorCode.ACCESS_DENIED, detail=str(exc))
    if isinstance(exc, ReviewConflict | IllegalTransition):
        return ApiError(ErrorCode.VERSION_CONFLICT, detail=str(exc))
    return ApiError(ErrorCode.INTERNAL_ERROR, detail=type(exc).__name__)


@router.post("/v1/knowledge/{knowledge_id}/review", summary="技术审核")
def review(
    knowledge_id: str,
    payload: ReviewRequest,
    context: PrincipalDep,
    conn: ConnDep,
) -> dict[str, Any]:
    context.require_scope("knowledge.review")
    version_id = _parse_uuid(knowledge_id, "knowledge_id")
    service = ReviewService(conn, context.tenant_id)

    try:
        if payload.decision == "PASS":
            snapshot = service.pass_review(version_id, context.principal_id, payload.reason)
        elif payload.decision == "REJECT":
            snapshot = service.reject(version_id, context.principal_id, payload.reason)
        else:
            snapshot = service.approve_internal(version_id, context.principal_id, payload.reason)
    except (VersionNotFound, ReviewConflict, IllegalTransition) as exc:
        raise _translate(exc) from exc

    return _view(snapshot)


@router.post("/v1/knowledge/{knowledge_id}/publish", summary="内部或对外发布")
def publish(
    knowledge_id: str,
    payload: PublishRequest,
    context: PrincipalDep,
    conn: ConnDep,
) -> dict[str, Any]:
    version_id = _parse_uuid(knowledge_id, "knowledge_id")
    service = ReviewService(conn, context.tenant_id)

    try:
        if payload.audience == "customer_public":
            context.require_scope("knowledge.publish_public")
            snapshot = service.publish_public(version_id, context.principal_id, payload.reason)
        else:
            context.require_scope("knowledge.publish")
            current = service.snapshot(version_id)
            if current.status is VersionStatus.REVIEWED:
                snapshot = service.approve_internal(
                    version_id, context.principal_id, payload.reason
                )
            elif current.status is VersionStatus.APPROVED_INTERNAL:
                # 幂等：重复发布为内部可用不产生新的审计动作
                snapshot = current
            else:
                raise ReviewConflict(f"当前状态 {current.status} 不可发布为内部可用")
    except (VersionNotFound, ReviewConflict, IllegalTransition) as exc:
        raise _translate(exc) from exc

    return _view(snapshot)


@router.post("/v1/knowledge/{knowledge_id}/revoke", summary="同步撤销授权与发布")
def revoke(
    knowledge_id: str,
    context: PrincipalDep,
    conn: ConnDep,
    reason: str | None = None,
) -> dict[str, Any]:
    context.require_scope("knowledge.revoke")
    version_id = _parse_uuid(knowledge_id, "knowledge_id")
    service = ReviewService(conn, context.tenant_id)

    try:
        snapshot = service.revoke(version_id, context.principal_id, reason)
    except (VersionNotFound, ReviewConflict, IllegalTransition) as exc:
        raise _translate(exc) from exc

    return _view(snapshot)


@router.post("/v1/knowledge/{knowledge_id}/discard", summary="作废未对外发布的版本")
def discard(
    knowledge_id: str,
    context: PrincipalDep,
    conn: ConnDep,
    reason: str | None = None,
) -> dict[str, Any]:
    """作废一个从未对外发布的版本（RAW/DRAFT/REVIEWED/APPROVED_INTERNAL）。

    权限沿用撤销（knowledge.revoke）：弃用只收回可用性，不授予发布资格
    （2026-11-05 L1 裁决 Q10）。与撤销的区别在于审计语义——弃用表示"从未公开过"。
    """
    context.require_scope("knowledge.revoke")
    version_id = _parse_uuid(knowledge_id, "knowledge_id")
    service = ReviewService(conn, context.tenant_id)

    try:
        snapshot = service.discard(version_id, context.principal_id, reason)
    except (VersionNotFound, ReviewConflict, IllegalTransition) as exc:
        raise _translate(exc) from exc

    return _view(snapshot)


__all__ = ["router"]
