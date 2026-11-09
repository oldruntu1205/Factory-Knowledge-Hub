"""培训端点（L1 §13.5）。

权限：
- 课程、题目、分配：`training_admin`
- 本人任务、提交测验、进度：`learner`

学员身份一律取令牌的 `sub`（外部主体），不接收请求体里的学员标识。
否则任何人只要改一个字段就能提交他人的卷子或查他人的成绩。
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any
from uuid import UUID

import psycopg
from fastapi import APIRouter, Depends, Request
from psycopg.types.json import Jsonb

from fkh.api.answer_exec import resolve_retrieval, run_answer
from fkh.api.db import get_conn
from fkh.api.errors import ApiError, ErrorCode
from fkh.api.models import (
    AssignmentCreateRequest,
    AttemptSubmitRequest,
    CourseCreateRequest,
    QuestionCreateRequest,
    SignoffRequest,
    TrainingAskRequest,
    TrainingFeedbackRequest,
)
from fkh.api.security import PrincipalDep
from fkh.config import Settings, get_settings
from fkh.domain.training import (
    CourseNotFound,
    MentorNotAssigned,
    TrainingError,
    TrainingService,
)

router = APIRouter(tags=["training"])

ConnDep = Annotated[psycopg.Connection[Any], Depends(get_conn)]


def _uuid(raw: str, field: str) -> UUID:
    try:
        return UUID(raw)
    except ValueError as exc:
        raise ApiError(ErrorCode.VALIDATION_ERROR, detail=f"{field} 不是合法 UUID") from exc


def _translate(exc: Exception) -> ApiError:
    if isinstance(exc, (CourseNotFound, MentorNotAssigned)):
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


@router.get("/v1/training/assignments/{assignment_id}/content", summary="读取课程正文本")
def content(
    assignment_id: str,
    request: Request,
    context: PrincipalDep,
    settings: Annotated[Settings, Depends(get_settings)],
    conn: ConnDep,
    cursor: str | None = None,
    limit: int = 5,
) -> dict[str, Any]:
    """按课程源返回批准正文的父块（分页），并落一条读取审计。

    可见性是交集（L1 2026-11-07）：任务属于本人 ∧ 课程 PUBLISHED ∧ 版本属于课程源 ∧
    内部身份 ∧ audience=internal ∧ APPROVED_INTERNAL ∧ scope 覆盖 ∧ 块 ACL 允许。
    """
    from fkh.authz.policy import is_external_customer

    context.require_scope("learner")
    service = TrainingService(conn, context.tenant_id)
    try:
        return service.content_page(
            assignment_id=_uuid(assignment_id, "assignment_id"),
            learner_subject_id=context.subject,
            cursor=cursor,
            limit=limit,
            cursor_key=settings.retrieval_hash_key or settings.jwt_secret or "",
            trace_id=str(getattr(request.state, "trace_id", "-")),
            viewer_allowed_scopes=context.allowed_scopes,
            viewer_is_external=is_external_customer(context.allowed_scopes),
            viewer_principal_id=context.principal_id,
            viewer_subject=context.to_subject(),
        )
    except CourseNotFound as exc:
        raise ApiError(ErrorCode.ACCESS_DENIED, detail=str(exc)) from exc
    except TrainingError as exc:
        raise ApiError(ErrorCode.VERSION_CONFLICT, detail=str(exc)) from exc


@router.post("/v1/training/practical-signoffs", summary="导师实操签核")
def practical_signoff(
    payload: SignoffRequest,
    context: PrincipalDep,
    conn: ConnDep,
) -> dict[str, Any]:
    """导师人工签核（Rev2 §13：AI 不得自动授予上岗资格）。

    签核人取自令牌 `sub`，请求体没有 assessor 字段——客户端无法替他人签核。
    """
    context.require_scope("mentor")
    service = TrainingService(conn, context.tenant_id)
    try:
        return service.sign_off(
            assignment_id=_uuid(payload.assignment_id, "assignment_id"),
            assessor_subject_id=context.subject,
            decision=payload.decision,
            evidence_uri=payload.evidence_uri,
        )
    except (CourseNotFound, MentorNotAssigned, TrainingError) as exc:
        raise _translate(exc) from exc


__all__ = ["router"]


TRAINING_SCOPE = "internal_training"


@router.post("/v1/training/ask", summary="培训带引用问答")
def training_ask(
    request: Request,
    payload: TrainingAskRequest,
    context: PrincipalDep,
    settings: Annotated[Settings, Depends(get_settings)],
    conn: ConnDep,
) -> dict[str, Any]:
    """学员带引用问答（Rev2 §13）。

    与 `/v1/knowledge/answer` **同一条闸门**（同一检索、同一引用白名单、同一一致性检查），
    区别只有两点：
    1. 先确认任务属于本人且课程仍可用（撤销即暂停，不是权限问题）；
    2. 知识范围固定为 `internal_training`，不接受调用方指定。
    """
    context.require_scope("learner")
    # 数据范围：学员必须被授予培训范围，否则这里就拒（不能等检索层再兜）
    if TRAINING_SCOPE not in context.allowed_scopes:
        raise ApiError(ErrorCode.ACCESS_DENIED, detail=f"缺少数据范围 {TRAINING_SCOPE}")

    service = TrainingService(conn, context.tenant_id)
    try:
        service.assert_can_ask(_uuid(payload.assignment_id, "assignment_id"), context.subject)
    except (CourseNotFound, TrainingError) as exc:
        raise _translate(exc) from exc

    # 培训问答只允许在当前课程源版本内召回与引用
    try:
        allowed_versions = service.approved_source_versions(
            _uuid(payload.assignment_id, "assignment_id"), context.subject
        )
    except (CourseNotFound, TrainingError) as exc:
        raise _translate(exc) from exc
    if not allowed_versions:
        raise ApiError(ErrorCode.VERSION_CONFLICT, detail="课程没有可用（已批准）的知识源")

    mode, embedder = resolve_retrieval(settings)
    return run_answer(
        conn,
        settings=settings,
        context=context,
        query=payload.question,
        knowledge_scope=TRAINING_SCOPE,
        language=payload.language,
        max_evidence=5,
        trace_id=str(getattr(request.state, "trace_id", "-")),
        embedder=embedder,
        retrieval_mode=mode,
        only_version_ids=allowed_versions,
        max_classification=2,
    )


TRAINING_FEEDBACK_CATEGORY = "TRAINING_FEEDBACK"


@router.post("/v1/training/feedback", status_code=201, summary="培训反馈")
def training_feedback(
    payload: TrainingFeedbackRequest,
    context: PrincipalDep,
    conn: ConnDep,
) -> dict[str, Any]:
    """学员对本人课程/问答的受控反馈，落为 `bad_cases`。

    - 任务必须属于本人且课程仍可用；他人任务与不存在统一 403（不泄露存在性）；
    - `trace_id` 可选；一旦提供，必须是本人、`internal_training` 范围的问答，
      否则 404；
    - `expected_behavior` 只接受封闭结构，不接收自由文本/原始 query；
    - 同一任务允许重复反馈；本轮不建独立课程反馈表，也不发 outbox 事件。
    """
    context.require_scope("learner")
    if TRAINING_SCOPE not in context.allowed_scopes:
        raise ApiError(ErrorCode.ACCESS_DENIED, detail=f"缺少数据范围 {TRAINING_SCOPE}")

    service = TrainingService(conn, context.tenant_id)
    assignment_id = _uuid(payload.assignment_id, "assignment_id")
    try:
        course = service.assert_can_ask(assignment_id, context.subject)
    except (CourseNotFound, TrainingError) as exc:
        raise _translate(exc) from exc

    query_id: UUID | None = None
    query_hash: str | None = None
    if payload.trace_id:
        row = conn.execute(
            """SELECT id, principal_id, query_hash, scope
                 FROM rag_queries
                WHERE tenant_id = %s AND trace_id = %s
                ORDER BY created_at DESC, id DESC
                LIMIT 1""",
            (context.tenant_id, payload.trace_id),
        ).fetchone()
        if (
            row is None
            or str(row[1]) != str(context.principal_id)
            or str(row[3]) != TRAINING_SCOPE
        ):
            # 与"不存在"不可区分：不泄露他人问答是否存在
            raise ApiError(ErrorCode.NOT_FOUND, detail="培训问答 trace 不存在")
        query_id = row[0]
        query_hash = str(row[2])

    bad_case_id = uuid.uuid4()
    redacted_case = {
        "kind": TRAINING_FEEDBACK_CATEGORY,
        "assignment_id": str(assignment_id),
        "course_id": str(course["course_id"]),
        "course_version": int(course["course_version"]),
        "trace_id": payload.trace_id,
        "query_hash": query_hash,
    }
    conn.execute(
        """INSERT INTO bad_cases
             (tenant_id, id, query_id, category, severity, redacted_case,
              expected_behavior, regression_status)
           VALUES (%s, %s, %s, %s, 'P2', %s, %s, 'OPEN')""",
        (
            context.tenant_id,
            bad_case_id,
            query_id,
            TRAINING_FEEDBACK_CATEGORY,
            Jsonb(redacted_case),
            Jsonb(payload.expected_behavior.model_dump(mode="json")),
        ),
    )
    return {
        "bad_case_id": str(bad_case_id),
        "assignment_id": str(assignment_id),
        "trace_id": payload.trace_id,
        "category": TRAINING_FEEDBACK_CATEGORY,
        "severity": "P2",
        "regression_status": "OPEN",
    }
