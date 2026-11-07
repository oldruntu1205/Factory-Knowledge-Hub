"""经验采集（语音访谈）端点 —— ADR-05 未签前的最小可信实现。

权限：
- 授权与访谈建立：`interview.create`
- 转写与草稿：`interview.create`（同一采集者）
- 审核：`interview.review`（必须是人，AI 不得自我批准）
- 查询：`interview.read`

硬约束（服务层保证）：无有效授权不能建访谈；转写是 RAW、不进检索；
撤回授权即删除转写并作废草稿。
"""

from __future__ import annotations

from typing import Annotated, Any, Literal
from uuid import UUID

import psycopg
from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from fkh.api.db import get_conn
from fkh.api.errors import ApiError, ErrorCode
from fkh.api.security import PrincipalDep
from fkh.domain.interview import InterviewError, InterviewNotFound, InterviewService

router = APIRouter(tags=["interview"])

ConnDep = Annotated[psycopg.Connection[Any], Depends(get_conn)]


class ConsentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subject_id: str = Field(min_length=1, max_length=200)
    method: Literal["ORAL_WITH_RECEIPT", "WRITTEN"]
    retention_days: int = Field(ge=1, le=3650)
    receipt_uri: str | None = Field(default=None, max_length=500)


class InterviewCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subject_id: str = Field(min_length=1, max_length=200)
    consent_id: str
    language: str = Field(default="zh-CN", max_length=16)


class TranscriptRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: str = Field(min_length=1, max_length=200_000)
    kind: Literal["MACHINE", "HUMAN_EDITED"] = "MACHINE"


class DraftReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["APPROVE", "REJECT"]
    note: str | None = Field(default=None, max_length=1000)


class PromoteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    domain: Literal["owner_experience", "quality", "training"] = "owner_experience"
    required_scope: Literal["internal_product", "internal_training"] = "internal_product"
    classification: int = Field(default=1, ge=0, le=2)


class WithdrawRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str | None = Field(default=None, max_length=500)


def _uuid(raw: str, field: str) -> UUID:
    try:
        return UUID(raw)
    except ValueError as exc:
        raise ApiError(ErrorCode.VALIDATION_ERROR, detail=f"{field} 不是合法 UUID") from exc


def _translate(exc: Exception) -> ApiError:
    if isinstance(exc, InterviewNotFound):
        return ApiError(ErrorCode.ACCESS_DENIED, detail=str(exc))
    if isinstance(exc, InterviewError):
        return ApiError(ErrorCode.VERSION_CONFLICT, detail=str(exc))
    return ApiError(ErrorCode.INTERNAL_ERROR, detail=type(exc).__name__)


@router.post("/v1/interviews/consents", status_code=201, summary="登记录音授权（本人授权）")
def grant_consent(
    payload: ConsentRequest, context: PrincipalDep, conn: ConnDep
) -> dict[str, Any]:
    context.require_scope("interview.create")
    service = InterviewService(conn, context.tenant_id)
    try:
        consent_id = service.grant_consent(
            subject_id=payload.subject_id,
            method=payload.method,
            retention_days=payload.retention_days,
            # 授权人取自令牌，不接受请求体自证（L1 2026-11-08）
            authenticated_subject=context.subject,
            receipt_uri=payload.receipt_uri,
        )
    except InterviewError as exc:
        raise _translate(exc) from exc
    return {"consent_id": str(consent_id)}


@router.post("/v1/interviews/consents/{consent_id}/revoke", summary="撤回录音授权")
def revoke_consent(
    consent_id: str, payload: WithdrawRequest, context: PrincipalDep, conn: ConnDep
) -> dict[str, Any]:
    context.require_scope("interview.create")
    service = InterviewService(conn, context.tenant_id)
    try:
        return service.revoke_consent(
            _uuid(consent_id, "consent_id"),
            actor_id=context.principal_id,
            actor_subject=context.subject,
            reason=payload.reason,
        )
    except (InterviewNotFound, InterviewError) as exc:
        raise _translate(exc) from exc


@router.post("/v1/interviews", status_code=201, summary="建立语音访谈任务（需有效授权）")
def create_interview(
    payload: InterviewCreateRequest, context: PrincipalDep, conn: ConnDep
) -> dict[str, Any]:
    context.require_scope("interview.create")
    service = InterviewService(conn, context.tenant_id)
    try:
        interview_id = service.create_interview(
            subject_id=payload.subject_id,
            consent_id=_uuid(payload.consent_id, "consent_id"),
            interviewer=context.subject,
            language=payload.language,
        )
    except (InterviewNotFound, InterviewError) as exc:
        raise _translate(exc) from exc
    return {"interview_id": str(interview_id)}


@router.post("/v1/interviews/{interview_id}/transcript", status_code=201, summary="登记转写（RAW，不进检索）")
def add_transcript(
    interview_id: str, payload: TranscriptRequest, context: PrincipalDep, conn: ConnDep
) -> dict[str, Any]:
    context.require_scope("interview.create")
    service = InterviewService(conn, context.tenant_id)
    try:
        transcript_id = service.add_transcript(
            _uuid(interview_id, "interview_id"),
            content=payload.content,
            actor=context.subject,
            kind=payload.kind,
        )
    except (InterviewNotFound, InterviewError) as exc:
        raise _translate(exc) from exc
    return {"transcript_id": str(transcript_id)}


@router.post("/v1/interviews/{interview_id}/drafts", status_code=201, summary="生成带时间戳的草稿")
def build_draft(interview_id: str, context: PrincipalDep, conn: ConnDep) -> dict[str, Any]:
    context.require_scope("interview.create")
    service = InterviewService(conn, context.tenant_id)
    try:
        draft_id = service.build_draft(_uuid(interview_id, "interview_id"), actor=context.subject)
    except (InterviewNotFound, InterviewError) as exc:
        raise _translate(exc) from exc
    return {"draft_id": str(draft_id)}


@router.post("/v1/interviews/drafts/{draft_id}/review", summary="人工审核草稿")
def review_draft(
    draft_id: str, payload: DraftReviewRequest, context: PrincipalDep, conn: ConnDep
) -> dict[str, Any]:
    context.require_scope("interview.review")
    service = InterviewService(conn, context.tenant_id)
    try:
        return service.review_draft(
            _uuid(draft_id, "draft_id"),
            reviewer=context.subject,
            decision=payload.decision,
            note=payload.note,
        )
    except (InterviewNotFound, InterviewError) as exc:
        raise _translate(exc) from exc


@router.post(
    "/v1/interviews/drafts/{draft_id}/promote",
    status_code=201,
    summary="把已批准草稿晋升为知识草稿（后续仍需审核/发布）",
)
def promote_draft(
    draft_id: str,
    payload: PromoteRequest,
    context: PrincipalDep,
    conn: ConnDep,
) -> dict[str, Any]:
    context.require_scope("knowledge.write")
    if payload.required_scope not in context.allowed_scopes:
        raise ApiError(
            ErrorCode.ACCESS_DENIED,
            detail=f"required_scope={payload.required_scope} 超出授权范围",
        )
    service = InterviewService(conn, context.tenant_id)
    try:
        return service.promote_draft(
            _uuid(draft_id, "draft_id"),
            actor_id=context.principal_id,
            actor_subject=context.subject,
            domain=payload.domain,
            required_scope=payload.required_scope,
            classification=payload.classification,
        )
    except (InterviewNotFound, InterviewError) as exc:
        raise _translate(exc) from exc


@router.get("/v1/interviews/{interview_id}", summary="查询访谈、转写与草稿状态")
def view_interview(interview_id: str, context: PrincipalDep, conn: ConnDep) -> dict[str, Any]:
    context.require_scope("interview.read")
    service = InterviewService(conn, context.tenant_id)
    try:
        # 本人可读；其他人需要 interview.review 才能读（对象级权限）
        allow_reviewer = "interview.review" in context.scopes
        return service.view(
            _uuid(interview_id, "interview_id"),
            viewer=context.subject,
            allow_reviewer=allow_reviewer,
        )
    except (InterviewNotFound, InterviewError) as exc:
        raise _translate(exc) from exc


__all__ = ["router"]
