"""培训端点（L1 §13.5）。

权限：
- 课程、题目、分配：`training_admin`
- 本人任务、提交测验、进度：`learner`

学员身份一律取令牌的 `sub`（外部主体），不接收请求体里的学员标识。
否则任何人只要改一个字段就能提交他人的卷子或查他人的成绩。
"""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

import psycopg
from fastapi import APIRouter, Depends

from fkh.api.db import get_conn
from fkh.api.errors import ApiError, ErrorCode
from fkh.api.models import (
    AssignmentCreateRequest,
    AttemptSubmitRequest,
    CourseCreateRequest,
    QuestionCreateRequest,
)
from fkh.api.security import PrincipalDep
from fkh.domain.training import CourseNotFound, TrainingError, TrainingService

router = APIRouter(tags=["training"])

ConnDep = Annotated[psycopg.Connection[Any], Depends(get_conn)]


def _uuid(raw: str, field: str) -> UUID:
    try:
        return UUID(raw)
    except ValueError as exc:
        raise ApiError(ErrorCode.VALIDATION_ERROR, detail=f"{field} 不是合法 UUID") from exc


def _translate(exc: Exception) -> ApiError:
    if isinstance(exc, CourseNotFound):
        # 与"无权限"不可区分，避免用错误码探测课程/任务是否存在
        return ApiError(ErrorCode.ACCESS_DENIED, detail=str(exc))
    if isinstance(exc, TrainingError):
        return ApiError(ErrorCode.VERSION_CONFLICT, detail=str(exc))
    return ApiError(ErrorCode.INTERNAL_ERROR, detail=type(exc).__name__)


def _course_view(snapshot: Any) -> dict[str, Any]:
    return {
        "course_id": str(snapshot.course_id),
        "role_id": str(snapshot.role_id),
        "title": snapshot.title,
        "status": snapshot.status,
        "version": snapshot.version,
        "published_at": snapshot.published_at.isoformat() if snapshot.published_at else None,
    }


@router.post("/v1/training/courses", summary="创建课程草稿")
def create_course(
    payload: CourseCreateRequest,
    context: PrincipalDep,
    conn: ConnDep,
) -> dict[str, Any]:
    context.require_scope("training_admin")
    service = TrainingService(conn, context.tenant_id)
    try:
        role_id = service.ensure_role(payload.role_code, payload.role_name)
        course_id = service.create_course(role_id=role_id, title=payload.title)
        for version_id in payload.knowledge_version_ids:
            service.add_source(course_id, _uuid(version_id, "knowledge_version_ids"))
        snapshot = service.snapshot(course_id)
    except (CourseNotFound, TrainingError) as exc:
        raise _translate(exc) from exc

    return _course_view(snapshot)


@router.post("/v1/training/courses/{course_id}/questions", summary="新增题目（内部）")
def create_question(
    course_id: str,
    payload: QuestionCreateRequest,
    context: PrincipalDep,
    conn: ConnDep,
) -> dict[str, Any]:
    context.require_scope("training_admin")
    service = TrainingService(conn, context.tenant_id)
    try:
        snapshot = service.snapshot(_uuid(course_id, "course_id"))
        question_id = service.create_question(
            course_id=snapshot.course_id,
            course_version=snapshot.version,
            question_type=payload.question_type,
            prompt=payload.prompt,
            answer_key=payload.answer_key,
            evidence_refs=payload.evidence_refs,
            status=payload.status,
        )
    except (CourseNotFound, TrainingError) as exc:
        raise _translate(exc) from exc

    return {"question_id": str(question_id), "course_version": snapshot.version}


@router.post("/v1/training/courses/{course_id}/publish", summary="发布课程")
def publish_course(
    course_id: str,
    context: PrincipalDep,
    conn: ConnDep,
) -> dict[str, Any]:
    context.require_scope("training_publisher")
    service = TrainingService(conn, context.tenant_id)
    try:
        snapshot = service.publish_course(_uuid(course_id, "course_id"))
    except (CourseNotFound, TrainingError) as exc:
        raise _translate(exc) from exc
    return _course_view(snapshot)


@router.post("/v1/training/assignments", summary="按岗位分配课程")
def create_assignment(
    payload: AssignmentCreateRequest,
    context: PrincipalDep,
    conn: ConnDep,
) -> dict[str, Any]:
    context.require_scope("training_admin")
    service = TrainingService(conn, context.tenant_id)
    try:
        assignment_id = service.assign(
            course_id=_uuid(payload.course_id, "course_id"),
            learner_subject_id=payload.learner_subject_id,
            cohort_id=payload.cohort_id,
        )
    except (CourseNotFound, TrainingError) as exc:
        raise _translate(exc) from exc
    return {"assignment_id": str(assignment_id)}


@router.get("/v1/training/me/assignments", summary="查询本人学习任务")
def my_assignments(context: PrincipalDep, conn: ConnDep) -> dict[str, Any]:
    context.require_scope("learner")
    service = TrainingService(conn, context.tenant_id)
    return {"assignments": service.my_assignments(context.subject)}


@router.post("/v1/training/attempts", summary="提交测验")
def submit_attempt(
    payload: AttemptSubmitRequest,
    context: PrincipalDep,
    conn: ConnDep,
) -> dict[str, Any]:
    context.require_scope("learner")
    service = TrainingService(conn, context.tenant_id)
    try:
        result = service.submit_attempt(
            assignment_id=_uuid(payload.assignment_id, "assignment_id"),
            learner_subject_id=context.subject,
            responses=payload.responses,
            idempotency_key=payload.idempotency_key,
        )
    except (CourseNotFound, TrainingError) as exc:
        raise _translate(exc) from exc
    return result


@router.get("/v1/training/assignments/{assignment_id}/progress", summary="学习进度")
def progress(
    assignment_id: str,
    context: PrincipalDep,
    conn: ConnDep,
) -> dict[str, Any]:
    context.require_scope("learner")
    service = TrainingService(conn, context.tenant_id)
    try:
        return service.progress(_uuid(assignment_id, "assignment_id"), context.subject)
    except (CourseNotFound, TrainingError) as exc:
        raise _translate(exc) from exc


__all__ = ["router"]
