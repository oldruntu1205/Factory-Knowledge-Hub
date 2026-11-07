"""培训：课程、分配、测验判分与进度。

设计边界（与 L1 §13 一致）：
- 课程发布必须绑定**已审批**知识版本，由数据库触发器强制，不是应用层自觉；
- 判分在服务端完成，客户端提交的分数一律忽略；客观题自动判分，主观题挂起待人工；
- 不实现"及格/通过"的自动判定 —— 及格线与补考次数属于 ADR-TRAIN-02，尚未裁决。
  这里只记录成绩与进度，状态推进留给签核环节；
- 学员标识用令牌的外部主体（sub），不是本地 principal id：培训对象可能尚未
  同步进 principals 表。
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

OBJECTIVE_TYPES = frozenset({"SINGLE_CHOICE", "MULTI_CHOICE", "TRUE_FALSE"})
SUBJECTIVE_TYPES = frozenset({"SHORT_ANSWER"})


class CourseStatus(StrEnum):
    DRAFT = "DRAFT"
    PUBLISHED = "PUBLISHED"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    ARCHIVED = "ARCHIVED"


class AssignmentStatus(StrEnum):
    ASSIGNED = "ASSIGNED"
    IN_PROGRESS = "IN_PROGRESS"
    PENDING_PRACTICAL = "PENDING_PRACTICAL"
    COMPLETED = "COMPLETED"
    REMEDIATION = "REMEDIATION"


class TrainingError(ValueError):
    """业务规则不满足。"""


class CourseNotFound(LookupError):
    pass


@dataclass(frozen=True, slots=True)
class Question:
    question_id: str
    question_type: str
    answer_key: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class GradeItem:
    question_id: str
    # None 表示主观题，未自动判分
    correct: bool | None


@dataclass(frozen=True, slots=True)
class GradeResult:
    objective_score: float | None
    grading_status: str
    items: tuple[GradeItem, ...]
    correct_count: int
    objective_count: int
    pending_count: int


@dataclass(frozen=True, slots=True)
class CourseSnapshot:
    course_id: uuid.UUID
    role_id: uuid.UUID
    title: str
    status: str
    version: int
    published_at: datetime | None


def _normalise(value: Any) -> Any:
    """把单选与多选统一成可比较形式。"""
    if isinstance(value, list | tuple | set | frozenset):
        return frozenset(str(item).strip().upper() for item in value)
    if isinstance(value, bool):
        return value
    return str(value).strip().upper() if value is not None else None


def _matches(question_type: str, expected: Any, actual: Any) -> bool:
    if question_type == "TRUE_FALSE":
        expected_flag: bool = (
            expected.strip().lower() in {"true", "1", "yes", "是", "对"}
            if isinstance(expected, str)
            else bool(expected)
        )
        actual_flag: bool = (
            actual.strip().lower() in {"true", "1", "yes", "是", "对"}
            if isinstance(actual, str)
            else bool(actual)
        )
        return expected_flag == actual_flag
    return bool(_normalise(expected) == _normalise(actual))


def grade(responses: Mapping[str, Any], questions: Sequence[Question]) -> GradeResult:
    """客观题自动判分。多选题按全对计分，不给部分分。

    不给部分分是有意的：知识考核里"漏选一半"与"全错"在业务上同样代表没掌握，
    给部分分只会让及格线变得难以解释。
    """
    items: list[GradeItem] = []
    correct = 0
    objective = 0
    pending = 0

    for question in questions:
        qid = question.question_id
        if question.question_type in SUBJECTIVE_TYPES:
            pending += 1
            items.append(GradeItem(qid, None))
            continue
        if question.question_type not in OBJECTIVE_TYPES:
            pending += 1
            items.append(GradeItem(qid, None))
            continue

        objective += 1
        expected = question.answer_key.get("correct")
        ok = _matches(question.question_type, expected, responses.get(qid))
        if ok:
            correct += 1
        items.append(GradeItem(qid, ok))

    score = round(correct / objective * 100, 2) if objective else None
    status = "PENDING" if pending else "AUTO_GRADED"
    return GradeResult(
        objective_score=score,
        grading_status=status,
        items=tuple(items),
        correct_count=correct,
        objective_count=objective,
        pending_count=pending,
    )


class TrainingService:
    def __init__(self, conn: psycopg.Connection[Any], tenant_id: uuid.UUID) -> None:
        self._conn = conn
        self._tenant = tenant_id
        self._bind()

    def _bind(self) -> None:
        self._conn.execute("SELECT set_config('app.tenant_id', %s, true)", (str(self._tenant),))

    # ---- 岗位与课程 --------------------------------------------------------
    def ensure_role(self, code: str, name: str) -> uuid.UUID:
        self._bind()
        row = self._conn.execute(
            """INSERT INTO training_roles (tenant_id, role_id, code, name)
               VALUES (%s, %s, %s, %s)
               ON CONFLICT (tenant_id, code) DO UPDATE SET name = EXCLUDED.name
               RETURNING role_id""",
            (self._tenant, uuid.uuid4(), code, name),
        ).fetchone()
        if row is None:
            raise TrainingError("岗位写入失败")
        return uuid.UUID(str(row[0]))

    def create_course(self, *, role_id: uuid.UUID, title: str) -> uuid.UUID:
        self._bind()
        row = self._conn.execute(
            """INSERT INTO training_courses
                 (tenant_id, course_id, role_id, title, status, version)
               VALUES (%s, %s, %s, %s, 'DRAFT', 1)
               RETURNING course_id""",
            (self._tenant, uuid.uuid4(), role_id, title),
        ).fetchone()
        if row is None:
            raise TrainingError("课程写入失败")
        return uuid.UUID(str(row[0]))

    def add_source(self, course_id: uuid.UUID, knowledge_version_id: uuid.UUID) -> None:
        self._bind()
        self._conn.execute(
            """INSERT INTO training_course_sources (tenant_id, course_id, knowledge_version_id)
               VALUES (%s, %s, %s)
               ON CONFLICT DO NOTHING""",
            (self._tenant, course_id, knowledge_version_id),
        )

    def create_question(
        self,
        *,
        course_id: uuid.UUID,
        course_version: int,
        question_type: str,
        prompt: str,
        answer_key: Mapping[str, Any],
        evidence_refs: Sequence[Mapping[str, Any]] = (),
        status: str = "DRAFT",
    ) -> uuid.UUID:
        self._bind()
        row = self._conn.execute(
            """INSERT INTO training_questions
                 (tenant_id, question_id, course_id, course_version, question_type,
                  prompt, answer_key, evidence_refs, status)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
               RETURNING question_id""",
            (
                self._tenant,
                uuid.uuid4(),
                course_id,
                course_version,
                question_type,
                prompt,
                Jsonb(dict(answer_key)),
                Jsonb([dict(ref) for ref in evidence_refs]),
                status,
            ),
        ).fetchone()
        if row is None:
            raise TrainingError("题目写入失败")
        return uuid.UUID(str(row[0]))

    def publish_course(self, course_id: uuid.UUID) -> CourseSnapshot:
        """发布课程。

        知识源是否已审批由数据库触发器强制；这里额外拒绝"存在草稿题目"的课程——
        半成品题目随课程一起发布，学员看到的就是没有答案依据的考题。
        """
        self._bind()
        draft_questions = self._conn.execute(
            """SELECT count(*) FROM training_questions
                WHERE tenant_id = %s AND course_id = %s AND status = 'DRAFT'""",
            (self._tenant, course_id),
        ).fetchone()
        if draft_questions is not None and int(draft_questions[0]) > 0:
            raise TrainingError("课程仍有草稿状态的题目，先审批或删除再发布")

        try:
            with self._conn.transaction():
                self._conn.execute(
                    """UPDATE training_courses SET status = 'PUBLISHED'
                        WHERE tenant_id = %s AND course_id = %s""",
                    (self._tenant, course_id),
                )
        except psycopg.errors.CheckViolation as exc:
            # 触发器会拒绝"无知识源"与"引用未审批版本"两种情形
            raise TrainingError(
                f"课程发布被数据库约束拒绝：{exc.diag.message_primary or '缺少已审批知识源'}"
            ) from exc
        return self.snapshot(course_id)

    def snapshot(self, course_id: uuid.UUID) -> CourseSnapshot:
        self._bind()
        row = self._conn.execute(
            """SELECT course_id, role_id, title, status, version, published_at
                 FROM training_courses WHERE tenant_id = %s AND course_id = %s""",
            (self._tenant, course_id),
        ).fetchone()
        if row is None:
            raise CourseNotFound(str(course_id))
        return CourseSnapshot(
            course_id=uuid.UUID(str(row[0])),
            role_id=uuid.UUID(str(row[1])),
            title=str(row[2]),
            status=str(row[3]),
            version=int(row[4]),
            published_at=row[5],
        )

    # ---- 分配 --------------------------------------------------------------
    def assign(
        self,
        *,
        course_id: uuid.UUID,
        learner_subject_id: str,
        cohort_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> uuid.UUID:
        self._bind()
        course = self.snapshot(course_id)
        if course.status != CourseStatus.PUBLISHED:
            raise TrainingError(f"只有已发布课程可分配，当前为 {course.status}")

        existing = self._conn.execute(
            """SELECT assignment_id FROM training_assignments
                WHERE tenant_id = %s AND course_id = %s AND learner_subject_id = %s""",
            (self._tenant, course_id, learner_subject_id),
        ).fetchone()
        if existing is not None:
            # 重复分配不报错：同一学员被重复指派是运营常态，返回既有任务即可
            return uuid.UUID(str(existing[0]))

        row = self._conn.execute(
            """INSERT INTO training_assignments
                 (tenant_id, assignment_id, course_id, learner_subject_id, cohort_id,
                  status, idempotency_key)
               VALUES (%s, %s, %s, %s, %s, 'ASSIGNED', %s)
               RETURNING assignment_id""",
            (
                self._tenant,
                uuid.uuid4(),
                course_id,
                learner_subject_id,
                cohort_id,
                idempotency_key,
            ),
        ).fetchone()
        if row is None:
            raise TrainingError("分配写入失败")
        return uuid.UUID(str(row[0]))

    def my_assignments(self, learner_subject_id: str) -> list[dict[str, Any]]:
        self._bind()
        rows = self._conn.execute(
            """SELECT a.assignment_id, a.course_id, c.title, c.version, a.status,
                      a.assigned_at, a.completed_at
                 FROM training_assignments a
                 JOIN training_courses c
                   ON c.tenant_id = a.tenant_id AND c.course_id = a.course_id
                WHERE a.tenant_id = %s AND a.learner_subject_id = %s
                  -- 课程因知识被撤销/弃用而转 NEEDS_REVIEW 后，学员侧不再展示该任务
                  AND c.status = 'PUBLISHED'
                ORDER BY a.assigned_at DESC""",
            (self._tenant, learner_subject_id),
        ).fetchall()
        return [
            {
                "assignment_id": str(row[0]),
                "course_id": str(row[1]),
                "title": str(row[2]),
                "course_version": int(row[3]),
                "status": str(row[4]),
                "assigned_at": row[5].isoformat() if row[5] else None,
                "completed_at": row[6].isoformat() if row[6] else None,
            }
            for row in rows
        ]

    # ---- 测验 --------------------------------------------------------------
    def submit_attempt(
        self,
        *,
        assignment_id: uuid.UUID,
        learner_subject_id: str,
        responses: Mapping[str, Any],
        idempotency_key: str,
    ) -> dict[str, Any]:
        """提交答卷。

        客户端传来的分数不被接受——签名里没有这个参数，从接口层面就杜绝了改分。
        幂等键唯一约束由数据库保证，并发重复提交只会产生一条记录。
        """
        self._bind()
        assignment = self._conn.execute(
            """SELECT a.course_id, a.learner_subject_id, c.version, c.status
                 FROM training_assignments a
                 JOIN training_courses c
                   ON c.tenant_id = a.tenant_id AND c.course_id = a.course_id
                WHERE a.tenant_id = %s AND a.assignment_id = %s""",
            (self._tenant, assignment_id),
        ).fetchone()
        if assignment is None:
            raise CourseNotFound(str(assignment_id))
        if str(assignment[1]) != learner_subject_id:
            raise TrainingError("只能提交本人的答卷")
        # 已分配不等于可以继续学：知识被撤销/弃用会让课程转 NEEDS_REVIEW，
        # 此时必须拒绝提交，而不是只挡住"新分配"（2026-11-05 L1 终审第 1 条）
        if str(assignment[3]) != "PUBLISHED":
            raise TrainingError(f"课程当前不可用（{assignment[3]}），暂停提交")

        existing = self._conn.execute(
            """SELECT attempt_id, objective_score, grading_status, submitted_at
                 FROM training_attempts
                WHERE tenant_id = %s AND assignment_id = %s AND idempotency_key = %s""",
            (self._tenant, assignment_id, idempotency_key),
        ).fetchone()
        if existing is not None:
            return {
                "attempt_id": str(existing[0]),
                "objective_score": float(existing[1]) if existing[1] is not None else None,
                "grading_status": str(existing[2]),
                "submitted_at": existing[3].isoformat() if existing[3] else None,
                "idempotent_replay": True,
            }

        course_id = uuid.UUID(str(assignment[0]))
        course_version = int(assignment[2])
        questions = self._approved_questions(course_id)
        result = grade(responses, questions)

        attempt_id = uuid.uuid4()
        self._conn.execute(
            """INSERT INTO training_attempts
                 (tenant_id, attempt_id, assignment_id, course_version, responses,
                  objective_score, grading_status, idempotency_key)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                self._tenant,
                attempt_id,
                assignment_id,
                course_version,
                Jsonb(dict(responses)),
                result.objective_score,
                result.grading_status,
                idempotency_key,
            ),
        )
        self._conn.execute(
            """UPDATE training_assignments
                  SET status = CASE WHEN status = 'ASSIGNED' THEN 'IN_PROGRESS' ELSE status END
                WHERE tenant_id = %s AND assignment_id = %s""",
            (self._tenant, assignment_id),
        )

        return {
            "attempt_id": str(attempt_id),
            "objective_score": result.objective_score,
            "grading_status": result.grading_status,
            "correct_count": result.correct_count,
            "objective_count": result.objective_count,
            "pending_count": result.pending_count,
            "items": [
                {"question_id": item.question_id, "correct": item.correct} for item in result.items
            ],
            "idempotent_replay": False,
        }

    def _approved_questions(self, course_id: uuid.UUID) -> list[Question]:
        """取该课程全部已审批题目。

        不按 course_version 过滤：发布时课程 version 会自增（触发器行为），
        题目是在发布前按当时版本创建的。若判分按新版本号查，题库会整体落空，
        表现成"交了卷但没有任何题目被判定"。

        题库在同一时刻只有一个活跃版本（题目一经 APPROVED 不再修改），
        历史追溯靠 attempts.course_version 记录提交时用的是哪一版课程。
        """
        rows = self._conn.execute(
            """SELECT question_id, question_type, answer_key
                 FROM training_questions
                WHERE tenant_id = %s AND course_id = %s AND status = 'APPROVED'
                ORDER BY created_at, question_id""",
            (self._tenant, course_id),
        ).fetchall()
        return [
            Question(
                question_id=str(row[0]),
                question_type=str(row[1]),
                answer_key=dict(row[2] or {}),
            )
            for row in rows
        ]

    def assert_can_ask(self, assignment_id: uuid.UUID, learner_subject_id: str) -> dict[str, Any]:
        """学员问答前置校验：任务属于本人，且课程仍可用。

        三条拒绝语义与既有端点一致：
        - 任务不存在 / 不属于本人 → `CourseNotFound`（对外统一 403，不泄露存在性）；
        - 课程因知识撤销转 NEEDS_REVIEW → `TrainingError`（409：任务暂停，不是权限问题）。
        """
        self._bind()
        row = self._conn.execute(
            """SELECT a.course_id, c.status, c.version, c.title
                 FROM training_assignments a
                 JOIN training_courses c
                   ON c.tenant_id = a.tenant_id AND c.course_id = a.course_id
                WHERE a.tenant_id = %s AND a.assignment_id = %s
                  AND a.learner_subject_id = %s""",
            (self._tenant, assignment_id, learner_subject_id),
        ).fetchone()
        if row is None:
            raise CourseNotFound(str(assignment_id))
        if str(row[1]) != "PUBLISHED":
            raise TrainingError(f"课程当前不可用（{row[1]}），问答暂停")
        return {
            "course_id": str(row[0]),
            "course_version": int(row[2]),
            "title": str(row[3]),
        }

    def sign_off(
        self,
        *,
        assignment_id: uuid.UUID,
        assessor_subject_id: str,
        decision: str,
        evidence_uri: str | None = None,
    ) -> dict[str, Any]:
        """导师实操签核（Rev2 §13：AI 不得自动授予上岗资格）。

        - 只有持 `mentor` scope 的主体能调用（路由层校验）；
        - 签核人必须是**人**（外部主体标识），不由系统代签；
        - PASS → 任务 COMPLETED；FAIL/RETRY → REMEDIATION（补训，不新建任务）。
        """
        if decision not in {"PASS", "FAIL", "RETRY"}:
            raise TrainingError(f"decision 非法：{decision}")
        self._bind()
        row = self._conn.execute(
            """SELECT a.course_id, c.status
                 FROM training_assignments a
                 JOIN training_courses c
                   ON c.tenant_id = a.tenant_id AND c.course_id = a.course_id
                WHERE a.tenant_id = %s AND a.assignment_id = %s""",
            (self._tenant, assignment_id),
        ).fetchone()
        if row is None:
            raise CourseNotFound(str(assignment_id))
        if str(row[1]) != "PUBLISHED":
            raise TrainingError(f"课程当前不可用（{row[1]}），暂停签核")

        signoff_id = uuid.uuid4()
        self._conn.execute(
            """INSERT INTO training_practical_signoffs
                 (tenant_id, signoff_id, assignment_id, assessor_subject_id, decision, evidence_uri)
               VALUES (%s, %s, %s, %s, %s, %s)""",
            (self._tenant, signoff_id, assignment_id, assessor_subject_id, decision, evidence_uri),
        )
        status = "COMPLETED" if decision == "PASS" else "REMEDIATION"
        self._conn.execute(
            """UPDATE training_assignments
                  SET status = %s,
                      completed_at = CASE WHEN %s = 'COMPLETED' THEN now() ELSE completed_at END
                WHERE tenant_id = %s AND assignment_id = %s""",
            (status, status, self._tenant, assignment_id),
        )
        return {
            "signoff_id": str(signoff_id),
            "assignment_id": str(assignment_id),
            "decision": decision,
            "assignment_status": status,
            "signed_at": None,
        }

    # ---- 进度 --------------------------------------------------------------
    def progress(self, assignment_id: uuid.UUID, learner_subject_id: str) -> dict[str, Any]:
        self._bind()
        row = self._conn.execute(
            """SELECT a.status, a.course_id, c.title, c.version, a.completed_at, c.status
                 FROM training_assignments a
                 JOIN training_courses c
                   ON c.tenant_id = a.tenant_id AND c.course_id = a.course_id
                WHERE a.tenant_id = %s AND a.assignment_id = %s AND a.learner_subject_id = %s""",
            (self._tenant, assignment_id, learner_subject_id),
        ).fetchone()
        if row is None:
            raise CourseNotFound(str(assignment_id))
        if str(row[5]) != "PUBLISHED":
            raise TrainingError(f"课程当前不可用（{row[5]}），进度查询暂停")

        attempts = self._conn.execute(
            """SELECT count(*), max(objective_score), max(submitted_at)
                 FROM training_attempts
                WHERE tenant_id = %s AND assignment_id = %s""",
            (self._tenant, assignment_id),
        ).fetchone()

        signoff = self._conn.execute(
            """SELECT decision, signed_at FROM training_practical_signoffs
                WHERE tenant_id = %s AND assignment_id = %s
                ORDER BY signed_at DESC LIMIT 1""",
            (self._tenant, assignment_id),
        ).fetchone()

        return {
            "assignment_id": str(assignment_id),
            "status": str(row[0]),
            "course_id": str(row[1]),
            "title": str(row[2]),
            "course_version": int(row[3]),
            "completed_at": row[4].isoformat() if row[4] else None,
            "attempts": int(attempts[0]) if attempts else 0,
            "best_score": float(attempts[1]) if attempts and attempts[1] is not None else None,
            "last_submitted_at": attempts[2].isoformat() if attempts and attempts[2] else None,
            "practical_decision": str(signoff[0]) if signoff else None,
            "practical_signed_at": signoff[1].isoformat() if signoff and signoff[1] else None,
        }
