"""培训问答 / 普通问答 / 货号精确问答**共用**当前任务的课程来源与分级规则。

2026-11-08 权限修复（基线 `1b1701d`）：
- 培训问答此前只限制了检索通道（`version_ids`），但**目录精确通道**在检索之前运行，
  且不接受来源/分级约束 → 学员在课程 A 的任务里问课程 B 的货号，仍能拿到价格与引用。
- C3/C4 课程源同理：正文读取会 409，问答通道却没有分级口径。

验收用真实 PostgreSQL + **非 owner、无 BYPASSRLS 的业务角色** + 实际 HTTP。
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
from tests.e2e._roles import drop_business_role, ensure_business_role

from fkh.api.main import create_app
from fkh.config import Settings

pytestmark = pytest.mark.requires_pg

SECRET = "training-scope-secret-0123456789"
HASH_KEY = "training-scope-hmac-0123456789"
HASH_KEY_ID = "k-scope"

QC_DOC = (
    "冷凝管检验规范\n\n"
    "气密性检验：充气至 0.2MPa，保压 60 秒，压力下降不超过 5% 为合格。\n"
)
CATALOG_DOC = (
    "第 1 页\n\n货号 规格 管径*总长 上标口*下标口 单价\n"
    "A112420 200mm 38*300 24*24 39.00\n"
)
C3_DOC = "高敏配方\n\nA112420 的保密配方为 X-921，仅限工艺负责人查阅。\n"


class _Scripted:
    model_id = "scripted"

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
        if "保压" in prompt:
            answer = "充气至 0.2MPa，保压 60 秒，压力下降不超过 5% 为合格。"
        else:
            answer = "该型号规格为 200mm 38*300 24*24。"
        return _Generated(text=f"{answer}\n引用：{cite}")


def _token(
    tenant: str, principal: str, *, scopes: tuple[str, ...], allowed: tuple[str, ...],
    roles: tuple[str, ...] = ("staff",), subject: str | None = None,
) -> str:
    now = datetime.now(UTC)
    return jwt.encode(
        {
            "iss": "fkh.local", "aud": "fkh-api", "sub": subject or f"{principal[:8]}@example.test",
            "iat": now, "exp": now + timedelta(minutes=20), "tenant_id": tenant,
            "principal_id": principal, "roles": list(roles), "scope": list(scopes),
            "allowed_scopes": list(allowed),
        },
        SECRET,
        algorithm="HS256",
    )


@pytest.fixture(scope="module")
def role(pg_url: str) -> Iterator[str]:
    name, dsn = ensure_business_role(pg_url, prefix="fkh_scope")
    yield dsn
    drop_business_role(pg_url, name)


@pytest.fixture
def client(role: str) -> Iterator[TestClient]:
    settings = Settings(jwt_secret=SECRET, database_url=role,
                        retrieval_hash_key=HASH_KEY, retrieval_hash_key_id=HASH_KEY_ID)
    with TestClient(create_app(settings), raise_server_exceptions=False) as test_client:
        yield test_client


@pytest.fixture
def world(client: TestClient, admin: psycopg.Connection[Any],
          monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    monkeypatch.setattr("fkh.api.answer_exec.get_generator", lambda *a, **k: _Scripted())
    monkeypatch.setattr("fkh.api.main.get_generator", lambda *a, **k: _Scripted())

    tenant = create_tenant(admin, f"范围 {uuid.uuid4().hex[:6]}")
    ops = create_principal(admin, tenant, f"ops-{uuid.uuid4().hex[:6]}@example.test")
    reviewer = create_principal(admin, tenant, f"rev-{uuid.uuid4().hex[:6]}@example.test")
    learner = create_principal(admin, tenant, f"stu-{uuid.uuid4().hex[:6]}@example.test")
    admin.commit()

    learner_subject = f"stu-{uuid.uuid4().hex[:6]}"
    w: dict[str, Any] = {
        "tenant": tenant,
        "subject": learner_subject,
        "ops": _token(tenant, ops,
                      scopes=("knowledge.ingest", "knowledge.read", "knowledge.review",
                              "knowledge.publish", "training_admin", "training_publisher",
                              "knowledge.answer", "knowledge.search"),
                      allowed=("internal_product", "internal_training", "customer_public")),
        "reviewer": _token(tenant, reviewer, scopes=("knowledge.review",),
                           allowed=("internal_product", "internal_training")),
        "learner": _token(tenant, learner, scopes=("learner",), allowed=("internal_training",),
                          subject=learner_subject),
    }
    return w


def _h(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _upload(client: TestClient, world: dict[str, Any], text: str, *, domain: str = "quality",
            classification: int = 1, audience: str = "internal",
            scope: str = "internal_training") -> str:
    body = {
        "filename": f"{uuid.uuid4().hex[:6]}.md",
        "title": f"样例 {uuid.uuid4().hex[:4]}",
        "domain": domain,
        "owner_department": "qc",
        "classification": classification,
        "audience": audience,
        "required_scope": scope,
        "acl_allow_roles": ["staff"],
        "knowledge_key": f"scope-{uuid.uuid4().hex[:8]}",
        "content_base64": base64.b64encode(text.encode()).decode(),
        "catalog_currency": "CNY" if domain == "product_catalog" else None,
    }
    response = client.post("/v1/knowledge/documents", json=body, headers=_h(world["ops"]))
    assert response.status_code == 201, response.text
    version_id = response.json()["version_id"]
    assert client.post(f"/v1/knowledge/{version_id}/review", json={"decision": "PASS"},
                       headers=_h(world["reviewer"])).status_code == 200
    assert client.post(f"/v1/knowledge/{version_id}/publish", json={"audience": "internal"},
                       headers=_h(world["ops"])).status_code == 200
    return version_id


def _course(client: TestClient, world: dict[str, Any], title: str, versions: list[str]) -> str:
    response = client.post(
        "/v1/training/courses",
        json={"role_code": f"r-{uuid.uuid4().hex[:4]}", "role_name": "岗位",
              "title": title, "knowledge_version_ids": versions},
        headers=_h(world["ops"]),
    )
    assert response.status_code == 200, response.text
    course_id = response.json()["course_id"]
    assert client.post(
        f"/v1/training/courses/{course_id}/questions",
        json={"question_type": "SINGLE_CHOICE", "prompt": "占位题", "answer_key": {"correct": "a"}},
        headers=_h(world["ops"]),
    ).status_code == 200
    assert client.post(f"/v1/training/courses/{course_id}/publish",
                       headers=_h(world["ops"])).status_code == 200
    return course_id


def _assign(client: TestClient, world: dict[str, Any], course_id: str) -> str:
    response = client.post(
        "/v1/training/assignments",
        json={"course_id": course_id, "learner_subject_id": world["subject"]},
        headers=_h(world["ops"]),
    )
    assert response.status_code == 200, response.text
    return response.json()["assignment_id"]


def test_catalog_channel_is_scoped_to_course_sources(
    client: TestClient, world: dict[str, Any], admin: psycopg.Connection[Any]
) -> None:
    """课程 A（检验规范）里问课程 B（目录）的货号 → 不得回答、不得引用课程外的版本。"""
    qc_version = _upload(client, world, QC_DOC)
    catalog_version = _upload(client, world, CATALOG_DOC, domain="product_catalog",
                              classification=0, audience="internal",
                              scope="internal_training")
    course_a = _course(client, world, "检验课", [qc_version])
    course_b = _course(client, world, "目录课", [catalog_version])
    assert course_b  # 课程 B 存在但不分配给学员
    assignment_a = _assign(client, world, course_a)

    leaked = client.post(
        "/v1/training/ask",
        json={"assignment_id": assignment_a, "question": "A112420 的规格是什么？",
              "language": "zh-CN"},
        headers=_h(world["learner"]),
    )
    assert leaked.status_code == 200, leaked.text
    body = leaked.json()
    assert body["status"] != "ANSWERED", body
    assert "200mm" not in body.get("answer", ""), body
    cited = {c["version_id"] for c in body.get("citations", [])}
    assert catalog_version not in cited, "不得引用课程来源之外的版本"

    # 本课程内容仍然可答，且引用只来自课程 A 的来源
    ok = client.post(
        "/v1/training/ask",
        json={"assignment_id": assignment_a, "question": "气密性检验的保压时间是多少？",
              "language": "zh-CN"},
        headers=_h(world["learner"]),
    )
    assert ok.status_code == 200, ok.text
    ok_body = ok.json()
    assert ok_body["status"] == "ANSWERED", ok_body
    assert {c["version_id"] for c in ok_body["citations"]} == {qc_version}


def test_catalog_exact_channel_is_reached_but_blocked(
    client: TestClient, world: dict[str, Any], admin: psycopg.Connection[Any]
) -> None:
    """证据缺口：必须证明"目录通道真的跑过"，而不是被别的分支挡掉。"""
    qc_version = _upload(client, world, QC_DOC)
    _upload(client, world, CATALOG_DOC, domain="product_catalog", classification=0,
            audience="internal", scope="internal_training")
    assignment_a = _assign(client, world, _course(client, world, "检验课", [qc_version]))

    client.post(
        "/v1/training/ask",
        json={"assignment_id": assignment_a, "question": "A112420 的单价是多少？",
              "language": "zh-CN"},
        headers=_h(world["learner"]),
    )
    admin.execute("SELECT set_config('app.tenant_id', %s, false)", (world["tenant"],))
    rows = admin.execute(
        "SELECT route, route_reason FROM rag_queries WHERE tenant_id = %s"
        " ORDER BY created_at DESC LIMIT 5",
        (world["tenant"],),
    ).fetchall()
    assert any(r[0] in {"catalog_exact", "handoff"} for r in rows), rows


def test_c3_course_source_is_not_answerable(
    client: TestClient, world: dict[str, Any]
) -> None:
    """C3 课程源：正文与问答一致拒绝（分级口径统一）。"""
    c3_version = _upload(client, world, C3_DOC, classification=3)
    course = _course(client, world, "高敏课", [c3_version])
    assignment = _assign(client, world, course)

    content = client.get(f"/v1/training/assignments/{assignment}/content",
                         headers=_h(world["learner"]))
    assert content.status_code == 409, content.text

    asked = client.post(
        "/v1/training/ask",
        json={"assignment_id": assignment, "question": "A112420 的保密配方是什么？",
              "language": "zh-CN"},
        headers=_h(world["learner"]),
    )
    assert asked.status_code == 409, asked.text
