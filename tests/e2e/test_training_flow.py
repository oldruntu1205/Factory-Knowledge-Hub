"""新人培训链路端到端验收（Rev2 §13）。

链路：分配课程 → 阅读批准内容 → 带引用问答 → 测验 → 导师签核 → 审计；
并覆盖学员隔离、撤销联动（课程转 NEEDS_REVIEW 后学员侧立即停用）。

全部用真实 PostgreSQL + 受限身份 + 实际 HTTP 接口。
"""

from __future__ import annotations

import base64
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
from fkh.config import Settings

pytestmark = pytest.mark.requires_pg

SECRET = "training-flow-secret-0123456789ab"
HASH_KEY = "training-flow-hmac-0123456789"
HASH_KEY_ID = "k-tf"

TRAINING_DOC = (
    "冷凝管安装与检验培训\n\n"
    "安装：将冷凝管竖直固定，下端进水、上端出水，水流方向与蒸汽方向相反。\n\n"
    "检验：充气至 0.2MPa，保压 60 秒，压力下降不超过 5% 为合格。\n"
)


def _token(
    tenant: str,
    principal: str,
    *,
    scopes: tuple[str, ...],
    allowed: tuple[str, ...],
    subject: str | None = None,
) -> str:
    now = datetime.now(UTC)
    payload = {
        "iss": "fkh.local",
        "aud": "fkh-api",
        "sub": subject or f"{principal[:8]}@example.test",
        "iat": now,
        "exp": now + timedelta(minutes=10),
        "tenant_id": tenant,
        "principal_id": principal,
        "roles": ["staff"],
        "scope": list(scopes),
        "allowed_scopes": list(allowed),
    }
    return jwt.encode(payload, SECRET, algorithm="HS256")


class _Scripted:
    """脚本化模型：让"问答是否走通"与模型质量解耦。"""

    model_id = "scripted"

    def __init__(self, answer: str) -> None:
        self._answer = answer

    def generate(self, prompt: str, *, max_tokens: int = 512, temperature: float = 0.0) -> Any:
        from dataclasses import dataclass as _dc

        @_dc
        class _Generated:
            text: str
            model_id: str = "scripted"

        if "意图分类器" in prompt:
            return _Generated(text="否")
        import re as _re

        found = _re.search(r'<evidence id="([0-9a-f-]{36})"', prompt)
        cite = found.group(1) if found else "无"
        return _Generated(text=f"{self._answer}\n引用：{cite}")


@pytest.fixture
def client(pg_url: str) -> Iterator[TestClient]:
    settings = Settings(
        jwt_secret=SECRET,
        database_url=pg_url,
        retrieval_hash_key=HASH_KEY,
        retrieval_hash_key_id=HASH_KEY_ID,
    )
    with TestClient(create_app(settings), raise_server_exceptions=False) as test_client:
        yield test_client


@pytest.fixture
def world(admin: psycopg.Connection[Any], pg_url: str) -> dict[str, Any]:
    tenant = create_tenant(admin, f"培训 {uuid.uuid4().hex[:6]}")
    admin_p = create_principal(admin, tenant, f"admin-{uuid.uuid4().hex[:6]}@example.test")
    learner_a = create_principal(admin, tenant, f"la-{uuid.uuid4().hex[:6]}@example.test")
    learner_b = create_principal(admin, tenant, f"lb-{uuid.uuid4().hex[:6]}@example.test")
    mentor = create_principal(admin, tenant, f"m-{uuid.uuid4().hex[:6]}@example.test")
    admin.commit()

    world: dict[str, Any] = {
        "tenant": tenant,
        "dsn": pg_url,
        "admin_token": _token(
            tenant,
            admin_p,
            scopes=(
                "knowledge.ingest", "knowledge.read", "knowledge.review", "knowledge.publish",
                "knowledge.revoke", "training_admin", "training_publisher", "learner",
            ),
            allowed=("internal_product", "internal_training"),
        ),
        "learner_a": f"learner-a-{uuid.uuid4().hex[:6]}",
        "learner_b": f"learner-b-{uuid.uuid4().hex[:6]}",
        "mentor_subject": f"mentor-{uuid.uuid4().hex[:6]}",
        "cohort": f"cohort-{uuid.uuid4().hex[:6]}",
        "other_cohort": f"cohort-{uuid.uuid4().hex[:6]}",
    }
    world["token_a"] = _token(
        tenant, learner_a, scopes=("learner",), allowed=("internal_training",),
        subject=world["learner_a"],
    )
    world["token_b"] = _token(
        tenant, learner_b, scopes=("learner",), allowed=("internal_training",),
        subject=world["learner_b"],
    )
    world["mentor_token"] = _token(
        tenant, mentor, scopes=("mentor",), allowed=("internal_training",),
        subject=world["mentor_subject"],
    )
    return world


def _grant_mentor(world: dict[str, Any], mentor_subject: str, *, cohort: str) -> None:
    import psycopg as _pg

    with _pg.connect(world["dsn"]) as conn:
        conn.execute("SELECT set_config('app.tenant_id', %s, false)", (world["tenant"],))
        conn.execute(
            """INSERT INTO training_mentor_scopes
                 (tenant_id, mentor_subject_id, cohort_id)
               VALUES (%s, %s, %s)
               ON CONFLICT DO NOTHING""",
            (world["tenant"], mentor_subject, cohort),
        )


def _h(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _setup_course(client: TestClient, world: dict[str, Any]) -> dict[str, str]:
    """上传培训知识 → 审核发布 → 建课程 → 分配两名学员。"""
    body = {
        "filename": "training.md",
        "title": "冷凝管安装与检验培训",
        "domain": "training",
        "owner_department": "hr",
        "classification": 1,
        "audience": "internal",
        "required_scope": "internal_training",
        "acl_allow_roles": ["staff"],
        "acl_deny_roles": [],
        "knowledge_key": f"training-{uuid.uuid4().hex[:6]}",
        "content_base64": base64.b64encode(TRAINING_DOC.encode()).decode(),
    }
    uploaded = client.post(
        "/v1/knowledge/documents", json=body, headers=_h(world["admin_token"])
    )
    assert uploaded.status_code == 201, uploaded.text
    version_id = uploaded.json()["version_id"]

    assert client.post(
        f"/v1/knowledge/{version_id}/review",
        json={"decision": "PASS"},
        headers=_h(world["admin_token"]),
    ).status_code == 200
    assert client.post(
        f"/v1/knowledge/{version_id}/publish",
        json={"audience": "internal"},
        headers=_h(world["admin_token"]),
    ).status_code == 200

    course = client.post(
        "/v1/training/courses",
        json={
            "role_code": f"installer-{uuid.uuid4().hex[:4]}",
            "role_name": "安装工",
            "title": "冷凝管安装与检验",
            "knowledge_version_ids": [version_id],
        },
        headers=_h(world["admin_token"]),
    )
    assert course.status_code == 200, course.text
    course_id = course.json()["course_id"]

    question = client.post(
        f"/v1/training/courses/{course_id}/questions",
        json={
            "question_type": "SINGLE_CHOICE",
            "prompt": "气密性测试的保压时间是多少？",
            "answer_key": {"correct": "60 秒"},
        },
        headers=_h(world["admin_token"]),
    )
    assert question.status_code == 200, question.text
    assert client.post(
        f"/v1/training/courses/{course_id}/publish", headers=_h(world["admin_token"])
    ).status_code == 200

    assignment_a = client.post(
        "/v1/training/assignments",
        json={"course_id": course_id, "learner_subject_id": world["learner_a"],
              "cohort_id": world["cohort"]},
        headers=_h(world["admin_token"]),
    )
    assignment_b = client.post(
        "/v1/training/assignments",
        json={"course_id": course_id, "learner_subject_id": world["learner_b"],
              "cohort_id": world["other_cohort"]},
        headers=_h(world["admin_token"]),
    )
    assert assignment_a.status_code == 200 and assignment_b.status_code == 200
    return {
        "version_id": version_id,
        "course_id": course_id,
        "assignment_a": assignment_a.json()["assignment_id"],
        "assignment_b": assignment_b.json()["assignment_id"],
    }


def test_learner_sees_only_own_assignments(client: TestClient, world: dict[str, Any]) -> None:
    ctx = _setup_course(client, world)
    mine = client.get("/v1/training/me/assignments", headers=_h(world["token_a"]))
    assert mine.status_code == 200
    ids = {item["assignment_id"] for item in mine.json()["assignments"]}
    assert ids == {ctx["assignment_a"]}, ids


def test_training_ask_answers_with_citation(
    client: TestClient, world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "fkh.api.answer_exec.get_generator",
        lambda *a, **k: _Scripted("气密性测试的保压时间为 60 秒。"),
    )
    ctx = _setup_course(client, world)
    response = client.post(
        "/v1/training/ask",
        json={
            "assignment_id": ctx["assignment_a"],
            "question": "气密性测试的保压时间是多少？",
            "language": "zh-CN",
        },
        headers=_h(world["token_a"]),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "ANSWERED", body
    assert body["citations"], body
    assert body["literal_support"]["supported"] is True, body


def test_training_ask_rejects_other_learners_assignment(
    client: TestClient, world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "fkh.api.answer_exec.get_generator", lambda *a, **k: _Scripted("60 秒。")
    )
    ctx = _setup_course(client, world)
    response = client.post(
        "/v1/training/ask",
        json={"assignment_id": ctx["assignment_b"], "question": "保压时间？", "language": "zh-CN"},
        headers=_h(world["token_a"]),
    )
    assert response.status_code == 403, "只能问自己的任务"


def test_training_ask_requires_training_scope(
    client: TestClient, world: dict[str, Any]
) -> None:
    ctx = _setup_course(client, world)
    # 学员只有产品范围，没有培训范围
    outsider = _token(
        world["tenant"],
        uuid.uuid4().hex,
        scopes=("learner",),
        allowed=("internal_product",),
        subject=world["learner_a"],
    )
    response = client.post(
        "/v1/training/ask",
        json={"assignment_id": ctx["assignment_a"], "question": "保压时间？", "language": "zh-CN"},
        headers=_h(outsider),
    )
    assert response.status_code == 403, response.text


def test_quiz_submit_and_mentor_signoff(
    client: TestClient, world: dict[str, Any]
) -> None:
    ctx = _setup_course(client, world)
    attempt = client.post(
        "/v1/training/attempts",
        json={
            "assignment_id": ctx["assignment_a"],
            "responses": {"1": "60 秒"},
            "idempotency_key": f"k-{uuid.uuid4().hex[:12]}",
        },
        headers=_h(world["token_a"]),
    )
    assert attempt.status_code == 200, attempt.text

    # 学员自己不能签核（没有 mentor scope）
    denied = client.post(
        "/v1/training/practical-signoffs",
        json={"assignment_id": ctx["assignment_a"], "decision": "PASS"},
        headers=_h(world["token_a"]),
    )
    assert denied.status_code == 403, denied.text

    # 未授权时先拒
    unassigned = client.post(
        "/v1/training/practical-signoffs",
        json={"assignment_id": ctx["assignment_a"], "decision": "PASS"},
        headers=_h(world["mentor_token"]),
    )
    assert unassigned.status_code == 403, unassigned.text

    # 授权导师到 cohort A（管理员连接写入台账）
    _grant_mentor(world, world["mentor_subject"], cohort=world["cohort"])

    # 已授权但任务在别的班组：仍拒（同租户越权）
    cross = client.post(
        "/v1/training/practical-signoffs",
        json={"assignment_id": ctx["assignment_b"], "decision": "PASS"},
        headers=_h(world["mentor_token"]),
    )
    assert cross.status_code == 403, cross.text

    # 学员自签：即使同时持 learner + mentor，也不能签自己
    self_token = _token(
        world["tenant"], uuid.uuid4().hex, scopes=("learner", "mentor"),
        allowed=("internal_training",), subject=world["learner_a"],
    )
    _grant_mentor(world, world["learner_a"], cohort=world["cohort"])
    self_sign = client.post(
        "/v1/training/practical-signoffs",
        json={"assignment_id": ctx["assignment_a"], "decision": "PASS"},
        headers=_h(self_token),
    )
    assert self_sign.status_code == 403, self_sign.text

    signed = client.post(
        "/v1/training/practical-signoffs",
        json={"assignment_id": ctx["assignment_a"], "decision": "PASS"},
        headers=_h(world["mentor_token"]),
    )
    assert signed.status_code == 200, signed.text
    assert signed.json()["assignment_status"] == "COMPLETED"

    progress = client.get(
        f"/v1/training/assignments/{ctx['assignment_a']}/progress",
        headers=_h(world["token_a"]),
    )
    assert progress.status_code == 200
    assert progress.json()["practical_decision"] == "PASS"


def test_revoke_knowledge_disables_course_immediately(
    client: TestClient, world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """撤销知识 → 课程转 NEEDS_REVIEW → 学员侧任务消失、问答与交卷同时被拒。"""
    monkeypatch.setattr(
        "fkh.api.answer_exec.get_generator", lambda *a, **k: _Scripted("60 秒。")
    )
    ctx = _setup_course(client, world)

    revoked = client.post(
        f"/v1/knowledge/{ctx['version_id']}/revoke?reason=training-test",
        headers=_h(world["admin_token"]),
    )
    assert revoked.status_code == 200, revoked.text

    listed = client.get("/v1/training/me/assignments", headers=_h(world["token_a"]))
    assert listed.json()["assignments"] == [], "撤销后学员侧不应再看到该任务"

    ask = client.post(
        "/v1/training/ask",
        json={"assignment_id": ctx["assignment_a"], "question": "保压时间？", "language": "zh-CN"},
        headers=_h(world["token_a"]),
    )
    assert ask.status_code == 409, (ask.status_code, ask.text)

    attempt = client.post(
        "/v1/training/attempts",
        json={
            "assignment_id": ctx["assignment_a"],
            "responses": {"1": "60 秒"},
            "idempotency_key": f"k-{uuid.uuid4().hex[:12]}",
        },
        headers=_h(world["token_a"]),
    )
    assert attempt.status_code == 409, (attempt.status_code, attempt.text)


def _content(client: TestClient, token: str, assignment_id: str, **params: Any) -> Any:
    return client.get(
        f"/v1/training/assignments/{assignment_id}/content",
        params=params,
        headers=_h(token),
    )


def test_content_returns_approved_blocks_with_audit(
    client: TestClient, world: dict[str, Any], pg_url: str
) -> None:
    ctx = _setup_course(client, world)
    response = _content(client, world["token_a"], ctx["assignment_a"])
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["course_id"] == ctx["course_id"]
    assert body["sources"], body
    assert body["sources"][0]["version_id"] == ctx["version_id"]
    assert body["sources"][0]["blocks"], body
    block = body["sources"][0]["blocks"][0]
    assert block["text"].strip(), body
    assert set(block) == {"chunk_id", "heading_path", "locator", "text", "token_count"}

    with psycopg.connect(pg_url) as conn:
        conn.execute("SELECT set_config('app.tenant_id', %s, false)", (world["tenant"],))
        row = conn.execute(
            "SELECT version_ids, chunk_ids FROM training_content_access_log"
            " WHERE tenant_id = %s ORDER BY occurred_at DESC LIMIT 1",
            (world["tenant"],),
        ).fetchone()
    assert row is not None, "读取正文必须落审计"
    assert [str(v) for v in row[0]] == [ctx["version_id"]]
    assert str(row[1][0]) == block["chunk_id"], "审计块集合必须与实际返回一致"


def test_content_rejects_other_learners_assignment(
    client: TestClient, world: dict[str, Any]
) -> None:
    ctx = _setup_course(client, world)
    response = _content(client, world["token_a"], ctx["assignment_b"])
    assert response.status_code == 403, response.text


def test_content_pagination_and_cursor_is_bound(
    client: TestClient, world: dict[str, Any]
) -> None:
    ctx = _setup_course(client, world)
    first = _content(client, world["token_a"], ctx["assignment_a"], limit=1)
    assert first.status_code == 200, first.text
    body = first.json()
    assert len(body["sources"][0]["blocks"]) == 1

    cursor = body.get("next_cursor")
    # 只有一个父块时没有下一页；用 limit=1 造出下一页需要多块，这里至少验证"无下一页=null"
    if cursor is None:
        assert body["sources"][0]["blocks"], body
    else:
        second = _content(client, world["token_a"], ctx["assignment_a"], limit=1, cursor=cursor)
        assert second.status_code == 200, second.text

    # 篡改游标
    tampered = _content(
        client, world["token_a"], ctx["assignment_a"], limit=1, cursor="AAAA.BBBB"
    )
    assert tampered.status_code == 409, tampered.text


def test_content_revoked_source_returns_409(
    client: TestClient, world: dict[str, Any]
) -> None:
    ctx = _setup_course(client, world)
    revoked = client.post(
        f"/v1/knowledge/{ctx['version_id']}/revoke?reason=content-test",
        headers=_h(world["admin_token"]),
    )
    assert revoked.status_code == 200, revoked.text
    after = _content(client, world["token_a"], ctx["assignment_a"])
    assert after.status_code == 409, after.text


def test_content_c3_is_not_released_by_default(
    client: TestClient, world: dict[str, Any]
) -> None:
    """分级 C3 暂无学员授权口径：整课读取被拒（L1 §4）。"""
    body = {
        "filename": "c3.md",
        "title": "C3 培训材料",
        "domain": "training",
        "owner_department": "hr",
        "classification": 3,
        "audience": "internal",
        "required_scope": "internal_training",
        "acl_allow_roles": ["staff"],
        "knowledge_key": f"c3-{uuid.uuid4().hex[:6]}",
        "content_base64": base64.b64encode(TRAINING_DOC.encode()).decode(),
    }
    uploaded = client.post(
        "/v1/knowledge/documents", json=body, headers=_h(world["admin_token"])
    )
    assert uploaded.status_code == 201, uploaded.text
    version_id = uploaded.json()["version_id"]
    client.post(
        f"/v1/knowledge/{version_id}/review",
        json={"decision": "PASS"},
        headers=_h(world["admin_token"]),
    )
    client.post(
        f"/v1/knowledge/{version_id}/publish",
        json={"audience": "internal"},
        headers=_h(world["admin_token"]),
    )
    course = client.post(
        "/v1/training/courses",
        json={
            "role_code": f"c3role-{uuid.uuid4().hex[:4]}",
            "role_name": "C3 岗位",
            "title": "C3 课程",
            "knowledge_version_ids": [version_id],
        },
        headers=_h(world["admin_token"]),
    )
    assert course.status_code == 200, course.text
    course_id = course.json()["course_id"]
    client.post(
        f"/v1/training/courses/{course_id}/questions",
        json={"question_type": "SINGLE_CHOICE", "prompt": "q", "answer_key": {"correct": "a"}},
        headers=_h(world["admin_token"]),
    )
    client.post(f"/v1/training/courses/{course_id}/publish", headers=_h(world["admin_token"]))
    assignment = client.post(
        "/v1/training/assignments",
        json={"course_id": course_id, "learner_subject_id": world["learner_a"]},
        headers=_h(world["admin_token"]),
    )
    assert assignment.status_code == 200, assignment.text
    response = _content(client, world["token_a"], assignment.json()["assignment_id"])
    assert response.status_code == 409, response.text


def test_ask_cannot_reach_another_course_content(
    client: TestClient, world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """同租户两门课：学员只能从**本任务课程源**召回，问不出另一门课的内容。"""
    monkeypatch.setattr(
        "fkh.api.answer_exec.get_generator",
        lambda *a, **k: _Scripted("课程 B 的答案是 999 秒。"),
    )
    ctx_a = _setup_course(client, world)

    # 课程 B：另一份培训材料 + 另一门课，分配给另一名学员
    other_doc = (
        "包装与入库培训\n\n"
        "包装：每支冷凝管单独套气泡袋，外箱标注货号与批次。\n\n"
        "入库：按货号分区存放，先进先出，湿度保持在 45% 以下。\n"
    )
    body = {
        "filename": "packing.md",
        "title": "包装与入库培训",
        "domain": "training",
        "owner_department": "hr",
        "classification": 1,
        "audience": "internal",
        "required_scope": "internal_training",
        "acl_allow_roles": ["staff"],
        "knowledge_key": f"pack-{uuid.uuid4().hex[:6]}",
        "content_base64": base64.b64encode(other_doc.encode()).decode(),
    }
    uploaded = client.post("/v1/knowledge/documents", json=body, headers=_h(world["admin_token"]))
    assert uploaded.status_code == 201, uploaded.text
    other_version = uploaded.json()["version_id"]
    client.post(f"/v1/knowledge/{other_version}/review", json={"decision": "PASS"},
                headers=_h(world["admin_token"]))
    client.post(f"/v1/knowledge/{other_version}/publish", json={"audience": "internal"},
                headers=_h(world["admin_token"]))
    course_b = client.post(
        "/v1/training/courses",
        json={"role_code": f"packer-{uuid.uuid4().hex[:4]}", "role_name": "包装工",
              "title": "包装与入库", "knowledge_version_ids": [other_version]},
        headers=_h(world["admin_token"]),
    )
    assert course_b.status_code == 200, course_b.text
    course_b_id = course_b.json()["course_id"]
    client.post(
        f"/v1/training/courses/{course_b_id}/questions",
        json={"question_type": "SHORT_ANSWER", "prompt": "湿度要求？", "answer_key": {"correct": "45%"}},
        headers=_h(world["admin_token"]),
    )
    client.post(f"/v1/training/courses/{course_b_id}/publish", headers=_h(world["admin_token"]))
    client.post(
        "/v1/training/assignments",
        json={"course_id": course_b_id, "learner_subject_id": world["learner_b"]},
        headers=_h(world["admin_token"]),
    )

    # 学员 A 在课程 A 的任务里问课程 B 的问题：不得引用 B 的版本
    response = client.post(
        "/v1/training/ask",
        json={"assignment_id": ctx_a["assignment_a"], "question": "包装入库的湿度要求是多少？",
              "language": "zh-CN"},
        headers=_h(world["token_a"]),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    cited = {c["version_id"] for c in body.get("citations", [])}
    assert other_version not in cited, "不得引用不属于本任务的课程源"
    if body["status"] == "ANSWERED":
        assert "45%" not in body["answer"], body
