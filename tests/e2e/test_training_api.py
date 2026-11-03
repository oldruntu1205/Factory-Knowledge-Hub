"""培训链路端到端：建课 → 绑定知识 → 出题 → 发布 → 分配 → 测验 → 进度。

关键约束不是"能跑通"，而是：
1. 客户端无法影响成绩（请求体没有分数字段，多传即 422）；
2. 学员只能提交本人答卷、查本人进度；
3. 课程发布必须绑定已审批知识；
4. 幂等键重复提交不重复判分。
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
import psycopg
import pytest
from fastapi.testclient import TestClient
from tests.conftest import create_principal, create_tenant

from fkh.api.main import create_app
from fkh.authz.policy import AclPolicy
from fkh.config import Settings
from fkh.domain.review import ReviewService
from fkh.workers.ingestion import IngestionRequest, ingest_document

pytestmark = pytest.mark.requires_pg

SECRET = "training-api-secret-0123456789abcdef"
LEARNER_SUB = "learner-001@factory.test"

CONTENT = """# 冷凝管基础知识

高硼硅玻璃冷凝管适用于回流与蒸馏，标准接口为 24/29。
不得用于氢氟酸环境。
"""


def _token(tenant_id: str, scopes: list[str], *, sub: str, roles: list[str] | None = None) -> str:
    now = datetime.now(UTC)
    return jwt.encode(
        {
            "iss": "fkh.local",
            "aud": "fkh-api",
            "sub": sub,
            "iat": now,
            "exp": now + timedelta(minutes=30),
            "tenant_id": tenant_id,
            "principal_id": str(uuid.uuid4()),
            "roles": roles if roles is not None else ["staff"],
            "scope": scopes,
            "allowed_scopes": ["internal_product"],
        },
        SECRET,
        algorithm="HS256",
    )


@pytest.fixture
def env(pg_url: str, admin: psycopg.Connection[Any]) -> Iterator[dict[str, Any]]:
    tenant_id = create_tenant(admin, f"培训-{uuid.uuid4().hex[:6]}")
    principal = create_principal(admin, tenant_id, f"trainer-{uuid.uuid4().hex[:6]}@example.test")

    request = IngestionRequest(
        source_id=f"train-{uuid.uuid4().hex[:8]}",
        filename="train.md",
        content=CONTENT.encode(),
        knowledge_key=f"train-{uuid.uuid4().hex[:8]}",
        title="冷凝管基础知识",
        domain="training",
        owner_department="tech",
        classification=1,
        audience="internal",
        required_scope="internal_training",
        acl_policy=AclPolicy.from_json({"allow": {"roles": ["staff"]}}),
        actor_id=uuid.UUID(principal),
    )
    ingested = ingest_document(admin, uuid.UUID(tenant_id), request)
    admin.commit()

    service = ReviewService(admin, uuid.UUID(tenant_id))
    service.pass_review(ingested.version_id, uuid.UUID(principal))
    service.approve_internal(ingested.version_id, uuid.UUID(principal))
    admin.commit()

    app = create_app(Settings(jwt_secret=SECRET, database_url=pg_url))
    with TestClient(app) as client:
        yield {
            "client": client,
            "tenant_id": tenant_id,
            "version_id": str(ingested.version_id),
            "admin_token": _token(
                tenant_id, ["training_admin", "training_publisher"], sub="trainer@factory.test"
            ),
            "learner_token": _token(tenant_id, ["learner"], sub=LEARNER_SUB),
        }


def _h(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _create_course(env: dict[str, Any], *, with_source: bool = True) -> str:
    response = env["client"].post(
        "/v1/training/courses",
        json={
            "role_code": f"sales-{uuid.uuid4().hex[:6]}",
            "role_name": "外贸销售",
            "title": "冷凝管基础",
            "knowledge_version_ids": [env["version_id"]] if with_source else [],
        },
        headers=_h(env["admin_token"]),
    )
    assert response.status_code == 200, response.text
    return response.json()["course_id"]


def _add_question(env: dict[str, Any], course_id: str, **overrides: Any) -> str:
    payload = {
        "question_type": "SINGLE_CHOICE",
        "prompt": "冷凝管标准接口是？",
        "answer_key": {"correct": "A"},
        **overrides,
    }
    response = env["client"].post(
        f"/v1/training/courses/{course_id}/questions",
        json=payload,
        headers=_h(env["admin_token"]),
    )
    assert response.status_code == 200, response.text
    return response.json()["question_id"]


def test_full_training_flow(env: dict[str, Any]) -> None:
    client = env["client"]
    course_id = _create_course(env)
    q1 = _add_question(env, course_id, answer_key={"correct": "A"})
    q2 = _add_question(
        env,
        course_id,
        question_type="MULTI_CHOICE",
        prompt="哪些属于适用范围？",
        answer_key={"correct": ["A", "C"]},
    )

    published = client.post(
        f"/v1/training/courses/{course_id}/publish", headers=_h(env["admin_token"])
    )
    assert published.status_code == 200, published.text
    assert published.json()["status"] == "PUBLISHED"
    assert published.json()["version"] == 2

    assigned = client.post(
        "/v1/training/assignments",
        json={"course_id": course_id, "learner_subject_id": LEARNER_SUB},
        headers=_h(env["admin_token"]),
    )
    assert assigned.status_code == 200, assigned.text
    assignment_id = assigned.json()["assignment_id"]

    mine = client.get("/v1/training/me/assignments", headers=_h(env["learner_token"]))
    assert mine.status_code == 200
    assert len(mine.json()["assignments"]) == 1

    attempt = client.post(
        "/v1/training/attempts",
        json={
            "assignment_id": assignment_id,
            "responses": {q1: "A", q2: ["A", "C"]},
            "idempotency_key": "attempt-key-0001",
        },
        headers=_h(env["learner_token"]),
    )
    assert attempt.status_code == 200, attempt.text
    body = attempt.json()
    assert body["objective_score"] == 100.0
    assert body["grading_status"] == "AUTO_GRADED"
    assert body["correct_count"] == 2

    progress = client.get(
        f"/v1/training/assignments/{assignment_id}/progress", headers=_h(env["learner_token"])
    )
    assert progress.status_code == 200, progress.text
    assert progress.json()["attempts"] == 1
    assert progress.json()["best_score"] == 100.0
    assert progress.json()["status"] == "IN_PROGRESS"


def test_client_cannot_submit_score(env: dict[str, Any]) -> None:
    """请求体没有分数字段，多传直接被 422 拒掉——接口层面杜绝改分。"""
    course_id = _create_course(env)
    _add_question(env, course_id)
    env["client"].post(f"/v1/training/courses/{course_id}/publish", headers=_h(env["admin_token"]))
    assignment_id = env["client"].post(
        "/v1/training/assignments",
        json={"course_id": course_id, "learner_subject_id": LEARNER_SUB},
        headers=_h(env["admin_token"]),
    ).json()["assignment_id"]

    response = env["client"].post(
        "/v1/training/attempts",
        json={
            "assignment_id": assignment_id,
            "responses": {},
            "idempotency_key": "score-inject-1",
            "objective_score": 100,
        },
        headers=_h(env["learner_token"]),
    )
    assert response.status_code == 422


def test_publish_without_knowledge_source_rejected(env: dict[str, Any]) -> None:
    course_id = _create_course(env, with_source=False)
    _add_question(env, course_id)
    response = env["client"].post(
        f"/v1/training/courses/{course_id}/publish", headers=_h(env["admin_token"])
    )
    assert response.status_code == 409


def test_publish_with_draft_question_rejected(env: dict[str, Any]) -> None:
    course_id = _create_course(env)
    _add_question(env, course_id, status="DRAFT")
    response = env["client"].post(
        f"/v1/training/courses/{course_id}/publish", headers=_h(env["admin_token"])
    )
    assert response.status_code == 409


def test_attempt_is_idempotent(env: dict[str, Any]) -> None:
    course_id = _create_course(env)
    q1 = _add_question(env, course_id)
    env["client"].post(f"/v1/training/courses/{course_id}/publish", headers=_h(env["admin_token"]))
    assignment_id = env["client"].post(
        "/v1/training/assignments",
        json={"course_id": course_id, "learner_subject_id": LEARNER_SUB},
        headers=_h(env["admin_token"]),
    ).json()["assignment_id"]

    payload = {
        "assignment_id": assignment_id,
        "responses": {q1: "A"},
        "idempotency_key": "idem-attempt-1",
    }
    first = env["client"].post("/v1/training/attempts", json=payload, headers=_h(env["learner_token"]))
    second = env["client"].post("/v1/training/attempts", json=payload, headers=_h(env["learner_token"]))

    assert first.status_code == 200 and second.status_code == 200
    assert first.json()["attempt_id"] == second.json()["attempt_id"]
    assert second.json()["idempotent_replay"] is True


def test_learner_cannot_submit_other_assignment(env: dict[str, Any]) -> None:
    course_id = _create_course(env)
    _add_question(env, course_id)
    env["client"].post(f"/v1/training/courses/{course_id}/publish", headers=_h(env["admin_token"]))
    assignment_id = env["client"].post(
        "/v1/training/assignments",
        json={"course_id": course_id, "learner_subject_id": "someone-else@factory.test"},
        headers=_h(env["admin_token"]),
    ).json()["assignment_id"]

    response = env["client"].post(
        "/v1/training/attempts",
        json={
            "assignment_id": assignment_id,
            "responses": {},
            "idempotency_key": "cross-learner-1",
        },
        headers=_h(env["learner_token"]),
    )
    assert response.status_code == 409


def test_duplicate_assignment_returns_existing(env: dict[str, Any]) -> None:
    course_id = _create_course(env)
    _add_question(env, course_id)
    env["client"].post(f"/v1/training/courses/{course_id}/publish", headers=_h(env["admin_token"]))

    first = env["client"].post(
        "/v1/training/assignments",
        json={"course_id": course_id, "learner_subject_id": LEARNER_SUB},
        headers=_h(env["admin_token"]),
    )
    second = env["client"].post(
        "/v1/training/assignments",
        json={"course_id": course_id, "learner_subject_id": LEARNER_SUB},
        headers=_h(env["admin_token"]),
    )
    assert first.json()["assignment_id"] == second.json()["assignment_id"]


def test_assign_requires_published_course(env: dict[str, Any]) -> None:
    course_id = _create_course(env)
    response = env["client"].post(
        "/v1/training/assignments",
        json={"course_id": course_id, "learner_subject_id": LEARNER_SUB},
        headers=_h(env["admin_token"]),
    )
    assert response.status_code == 409


def test_progress_of_other_learner_denied(env: dict[str, Any]) -> None:
    course_id = _create_course(env)
    _add_question(env, course_id)
    env["client"].post(f"/v1/training/courses/{course_id}/publish", headers=_h(env["admin_token"]))
    assignment_id = env["client"].post(
        "/v1/training/assignments",
        json={"course_id": course_id, "learner_subject_id": "other@factory.test"},
        headers=_h(env["admin_token"]),
    ).json()["assignment_id"]

    response = env["client"].get(
        f"/v1/training/assignments/{assignment_id}/progress", headers=_h(env["learner_token"])
    )
    assert response.status_code == 403


def test_short_answer_left_for_human(env: dict[str, Any]) -> None:
    """主观题不自动判分，整体状态置为 PENDING，等人工复核。"""
    course_id = _create_course(env)
    q1 = _add_question(env, course_id)
    _add_question(env, course_id, question_type="SHORT_ANSWER", prompt="简述回流原理", answer_key={})
    env["client"].post(f"/v1/training/courses/{course_id}/publish", headers=_h(env["admin_token"]))
    assignment_id = env["client"].post(
        "/v1/training/assignments",
        json={"course_id": course_id, "learner_subject_id": LEARNER_SUB},
        headers=_h(env["admin_token"]),
    ).json()["assignment_id"]

    response = env["client"].post(
        "/v1/training/attempts",
        json={
            "assignment_id": assignment_id,
            "responses": {q1: "A"},
            "idempotency_key": "short-answer-1",
        },
        headers=_h(env["learner_token"]),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["grading_status"] == "PENDING"
    assert body["pending_count"] == 1
    assert body["objective_score"] == 100.0
