"""受控证据定位端点（L1 §6.2，权限 `evidence.read`）。

对外只有两种结果：
- 200 + 证据定位与正文；
- 403 统一文案（不存在 / 跨租户 / 状态不可见 / scope 不符 / ACL 拒绝 全部同码同 body）。

内部拒绝原因只进审计表，不进响应，否则错误码会被用来探测资源（L1 §6.1）。
"""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID, uuid4

import psycopg
from fastapi import APIRouter, Depends, Request

from fkh.api.db import get_conn
from fkh.api.errors import ApiError, ErrorCode
from fkh.api.security import PrincipalDep
from fkh.domain.evidence import EvidenceNotAvailable, EvidenceService

router = APIRouter(tags=["evidence"])

ConnDep = Annotated[psycopg.Connection[Any], Depends(get_conn)]


def _parse_uuid(raw: str) -> UUID:
    try:
        return UUID(raw)
    except ValueError as exc:
        raise ApiError(ErrorCode.VALIDATION_ERROR, detail="evidence_id 不是合法 UUID") from exc


@router.get("/v1/evidence/{evidence_id}", summary="受控证据定位")
def locate_evidence(
    evidence_id: str,
    request: Request,
    context: PrincipalDep,
    conn: ConnDep,
) -> dict[str, Any]:
    context.require_scope("evidence.read")
    chunk_id = _parse_uuid(evidence_id)
    trace_id = str(getattr(request.state, "trace_id", uuid4().hex))

    subject = context.to_subject()

    try:
        view = EvidenceService(conn, context.tenant_id).resolve(
            chunk_id,
            subject=subject,
            allowed_scopes=context.allowed_scopes,
            trace_id=trace_id,
        )
    except EvidenceNotAvailable as exc:
        raise ApiError(ErrorCode.ACCESS_DENIED, detail=f"evidence denied: {exc.reason.value}") from exc

    return {
        "evidence_id": str(view.evidence_id),
        "knowledge_id": str(view.document_id),
        "version_id": str(view.version_id),
        "status": view.status,
        "classification": view.classification,
        "audience": view.audience,
        "locator": view.locator,
        "heading_path": list(view.heading_path) if view.heading_path is not None else None,
        "text": view.text,
        "text_level": view.text_level,
        "text_evidence_id": str(view.text_evidence_id),
        "signed_url": view.signed_url,
        "signed_url_state": view.signed_url_state,
        "trace_id": trace_id,
    }


__all__ = ["router"]
