"""关键 HTTP 验收：**非 owner、无 BYPASSRLS 的业务角色**。

2026-11-07 权限收口第 3 条：迁移与造数用管理员连接，API 必须用业务角色跑——
否则 owner 身份会绕过 RLS，测出来的"隔离"是假的。

本文件在真实 PostgreSQL 上：
1. 用管理员连接建角色 `fkh_app`（LOGIN、非 superuser、非 owner、无 BYPASSRLS）并授予 DML 权限；
2. 迁移与造数继续用管理员连接；
3. API 用 `fkh_app` 连接，覆盖：目录命中、转人工审计、培训问答、导师签核、跨租户拒绝；
4. 直接以 `fkh_app` 查询断言 RLS 真的生效（未绑定租户时看不到任何行）。
"""

from __future__ import annotations

import base64
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlparse

import jwt
import psycopg
import pytest
from fastapi.testclient import TestClient
from tests.conftest import create_principal, create_tenant

from fkh.api.main import create_app
from fkh.config import Settings

pytestmark = pytest.mark.requires_pg

SECRET = "business-role-secret-0123456789ab"
HASH_KEY = "business-role-hmac-0123456789"
HASH_KEY_ID = "k-biz"
ROLE = "fkh_app_test"

CATALOG = (
    "第 1 页\n\n货号 规格 管径*总长 上标口*下标口 单价\n"
    "A112420 200mm 38*300 24*24 39.00\n"
)
TRAINING = (
    "检验培训\n\n"
    "气密性检验：充气至 0.2MPa，保压 60 秒，压力下降不超过 5% 为合格。\n"
)


def _app_dsn(admin_url: str) -> str:
    parsed = urlparse(admin_url)
    return f"postgresql://{ROLE}@{parsed.hostname}:{parsed.port or 5432}{parsed.path}"


def _token(
    tenant: str, principal: str, *, scopes: tuple[str, ...], allowed: tuple[str, ...],
    roles: tuple[str, ...] = ("staff",), subject: str | None = None,
) -> str:
    now = datetime.now(UTC)
    payload = {
        "iss": "fkh.local", "aud": "fkh-api", "sub": subject or f"{principal[:8]}@example.test",
        "iat": now, "exp": now + timedelta(minutes=10),
        "tenant_id": tenant, "principal_id": principal,
        "roles": list(roles), "scope": list(scopes), "allowed_scopes": list(allowed),
    }
    return jwt.encode(payload, SECRET, algorithm="HS256")


class _Scripted:
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


@pytest.fixture(scope="module")
def business_role(pg_url: str) -> Iterator[str]:
    """建一个非 owner、无 BYPASSRLS 的业务角色，并授予 DML 权限。"""
    with psycopg.connect(pg_url, autocommit=True) as admin:
        admin.execute(f"DROP ROLE IF EXISTS {ROLE}")
        admin.execute(f"CREATE ROLE {ROLE} LOGIN")
        admin.execute(f"GRANT USAGE ON SCHEMA public TO {ROLE}")
        admin.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {ROLE}")
        admin.execute(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {ROLE}")
        admin.execute(f"GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA public TO {ROLE}")
        row = admin.execute(
            "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = %s", (ROLE,)
        ).fetchone()
        owner = admin.execute(
            """SELECT tableowner FROM pg_tables WHERE schemaname='public'
                AND tablename='knowledge_chunks'"""
        ).fetchone()
    assert row == (False, False), "业务角色必须既不是 superuser 也不是 BYPASSRLS"
    assert owner is not None and owner[0] != ROLE, "业务角色不得是表 owner"
    yield _app_dsn(pg_url)
    with psycopg.connect(pg_url, autocommit=True) as admin:
        admin.execute(f"DROP OWNED BY {ROLE}")
        admin.execute(f"DROP ROLE IF EXISTS {ROLE}")


@pytest.fixture
def api(business_role: str) -> Iterator[TestClient]:
    settings = Settings(
        jwt_secret=SECRET,
        database_url=business_role,
        retrieval_hash_key=HASH_KEY,
        retrieval_hash_key_id=HASH_KEY_ID,
    )
    import os

    raise_exc = os.getenv("FKH_RAISE") == "1"
    with TestClient(create_app(settings), raise_server_exceptions=raise_exc) as test_client:
        yield test_client


@pytest.fixture
def world(admin: psycopg.Connection[Any], pg_url: str) -> dict[str, Any]:
    tenant_a = create_tenant(admin, f"业务A {uuid.uuid4().hex[:6]}")
    tenant_b = create_tenant(admin, f"业务B {uuid.uuid4().hex[:6]}")
    ops = create_principal(admin, tenant_a, f"ops-{uuid.uuid4().hex[:6]}@example.test")
    reviewer = create_principal(admin, tenant_a, f"rev-{uuid.uuid4().hex[:6]}@example.test")
    publisher = create_principal(admin, tenant_a, f"pub-{uuid.uuid4().hex[:6]}@example.test")
    learner = create_principal(admin, tenant_a, f"stu-{uuid.uuid4().hex[:6]}@example.test")
    mentor = create_principal(admin, tenant_a, f"ment-{uuid.uuid4().hex[:6]}@example.test")
    outsider = create_principal(admin, tenant_b, f"out-{uuid.uuid4().hex[:6]}@example.test")
    customer = create_principal(admin, tenant_a, f"buyer-{uuid.uuid4().hex[:6]}@example.test")
    admin.commit()
    return {
        "dsn": pg_url,
        "tenant_a": tenant_a,
        "tenant_b": tenant_b,
        "learner_subject": f"stu-{uuid.uuid4().hex[:6]}",
        "mentor_subject": f"ment-{uuid.uuid4().hex[:6]}",
        "learner_principal": learner,
        "mentor_principal": mentor,
        "cohort": f"co-{uuid.uuid4().hex[:6]}",
        "ops": _token(tenant_a, ops,
                      scopes=("knowledge.ingest", "knowledge.read", "knowledge.review",
                              "knowledge.publish", "knowledge.answer", "knowledge.search",
                              "training_admin"),
                      allowed=("internal_product", "customer_public", "internal_training")),
        "reviewer": _token(tenant_a, reviewer, scopes=("knowledge.review",),
                           allowed=("internal_training", "customer_public")),
        "publisher": _token(tenant_a, publisher, scopes=("knowledge.publish_public",),
                            allowed=("customer_public",), roles=("public_approver",)),
        "learner": _token(tenant_a, learner, scopes=("learner",),
                          allowed=("internal_training",), subject=f"stu-{uuid.uuid4().hex[:6]}"),
        "mentor": _token(tenant_a, mentor, scopes=("mentor",),
                         allowed=("internal_training",), subject=f"ment-{uuid.uuid4().hex[:6]}"),
        "outsider": _token(tenant_b, outsider, scopes=("knowledge.answer", "knowledge.search"),
                           allowed=("internal_product",)),
        "customer": _token(tenant_a, customer,
                           scopes=("knowledge.answer", "knowledge.search", "evidence.read"),
                           allowed=("customer_public",), roles=("customer",),
                           subject=f"buyer-{uuid.uuid4().hex[:6]}"),
    }


def _h(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _publish_public(api: TestClient, world: dict[str, Any], version_id: str) -> None:
    assert api.post(f"/v1/knowledge/{version_id}/review", json={"decision": "PASS"},
                    headers=_h(world["reviewer"])).status_code == 200
    assert api.post(f"/v1/knowledge/{version_id}/publish", json={"audience": "internal"},
                    headers=_h(world["ops"])).status_code == 200
    response = api.post(f"/v1/knowledge/{version_id}/publish",
                        json={"audience": "customer_public"},
                        headers=_h(world["publisher"]))
    assert response.status_code == 200, response.text


def test_rls_is_enforced_for_business_role(business_role: str) -> None:
    """未绑定租户时，业务角色看不到任何知识块（owner 会看到全部——这正是要排除的假象）。"""
    with psycopg.connect(business_role) as conn:
        visible = conn.execute("SELECT count(*) FROM knowledge_chunks").fetchone()[0]
    assert visible == 0, "业务角色在未绑定租户时不应看到任何行"


def test_catalog_hit_and_handoff_audit_under_business_role(
    api: TestClient, world: dict[str, Any], admin: psycopg.Connection[Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("fkh.api.main.get_generator",
                        lambda *a, **k: _Scripted("规格为 200mm 38*300 24*24。"))
    body = {
        "filename": "c0.md", "title": "对外目录", "domain": "product_catalog",
        "owner_department": "sales", "classification": 0, "audience": "customer_public",
        "required_scope": "customer_public", "acl_allow_roles": ["customer", "staff"],
        "knowledge_key": f"c0-{uuid.uuid4().hex[:6]}",
        "content_base64": base64.b64encode(CATALOG.encode()).decode(),
        "catalog_currency": "CNY",
    }
    uploaded = api.post("/v1/knowledge/documents", json=body, headers=_h(world["ops"]))
    assert uploaded.status_code == 201, uploaded.text
    version_id = uploaded.json()["version_id"]
    _publish_public(api, world, version_id)

    customer = world["customer"]
    hit = api.post("/v1/knowledge/answer",
                   json={"query": "A112420 的规格是什么？", "knowledge_scope": "customer_public",
                         "language": "zh-CN", "max_evidence": 5},
                   headers=_h(customer))
    assert hit.status_code == 200, hit.text
    assert hit.json()["status"] == "ANSWERED", hit.json()
    assert "200mm" in hit.json()["answer"]
    assert "39.00" not in hit.json()["answer"], "规格问答不得带价格"

    handoff = api.post("/v1/knowledge/answer",
                       json={"query": "A112420 的单价是多少？", "knowledge_scope": "customer_public",
                             "language": "zh-CN", "max_evidence": 5},
                       headers=_h(customer))
    assert handoff.status_code == 200, handoff.text
    assert handoff.json()["status"] == "HUMAN_REVIEW_REQUIRED"
    assert handoff.json()["refusal_code"] == "HUMAN_REVIEW_REQUIRED"

    admin.execute("SELECT set_config('app.tenant_id', %s, false)", (world["tenant_a"],))
    routes = [
        row[0]
        for row in admin.execute(
            "SELECT route FROM rag_queries WHERE tenant_id = %s ORDER BY created_at",
            (world["tenant_a"],),
        ).fetchall()
    ]
    assert "catalog_exact" in routes, routes
    assert "handoff" in routes, routes


def test_training_ask_and_mentor_signoff_under_business_role(
    api: TestClient, world: dict[str, Any], admin: psycopg.Connection[Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("fkh.api.answer_exec.get_generator",
                        lambda *a, **k: _Scripted("保压时间为 60 秒。"))
    body = {
        "filename": "train.md", "title": "检验培训", "domain": "training",
        "owner_department": "hr", "classification": 1, "audience": "internal",
        "required_scope": "internal_training", "acl_allow_roles": ["staff"],
        "knowledge_key": f"tr-{uuid.uuid4().hex[:6]}",
        "content_base64": base64.b64encode(TRAINING.encode()).decode(),
    }
    uploaded = api.post("/v1/knowledge/documents", json=body, headers=_h(world["ops"]))
    assert uploaded.status_code == 201, uploaded.text
    version_id = uploaded.json()["version_id"]
    assert api.post(f"/v1/knowledge/{version_id}/review", json={"decision": "PASS"},
                    headers=_h(world["reviewer"])).status_code == 200
    assert api.post(f"/v1/knowledge/{version_id}/publish", json={"audience": "internal"},
                    headers=_h(world["ops"])).status_code == 200

    course = api.post("/v1/training/courses",
                      json={"role_code": f"insp-{uuid.uuid4().hex[:4]}", "role_name": "检验员",
                            "title": "检验培训课", "knowledge_version_ids": [version_id]},
                      headers=_h(world["ops"]))
    assert course.status_code == 200, course.text
    course_id = course.json()["course_id"]
    api.post(f"/v1/training/courses/{course_id}/questions",
             json={"question_type": "SINGLE_CHOICE", "prompt": "保压时间？",
                   "answer_key": {"correct": "60 秒"}}, headers=_h(world["ops"]))
    # 课程发布需要 training_publisher；业务角色下同样校验
    with psycopg.connect(world['dsn']) as owner_conn:  # 用管理员直接置发布态，避免为本片扩 scope
        owner_conn.execute("SELECT set_config('app.tenant_id', %s, false)", (world["tenant_a"],))
        owner_conn.execute(
            "UPDATE training_courses SET status='PUBLISHED', published_at=now() WHERE tenant_id=%s AND course_id=%s",
            (world["tenant_a"], course_id),
        )
        owner_conn.commit()

    assignment = api.post("/v1/training/assignments",
                          json={"course_id": course_id, "learner_subject_id": world["learner_subject"],
                                "cohort_id": world["cohort"]},
                          headers=_h(world["ops"]))
    assert assignment.status_code == 200, assignment.text
    assignment_id = assignment.json()["assignment_id"]
    world["learner"] = _token(world["tenant_a"], world["learner_principal"],
                              scopes=("learner",), allowed=("internal_training",),
                              subject=world["learner_subject"])

    asked = api.post("/v1/training/ask",
                     json={"assignment_id": assignment_id, "question": "保压时间是多少？",
                           "language": "zh-CN"},
                     headers=_h(world["learner"]))
    assert asked.status_code == 200, asked.text
    assert asked.json()["status"] == "ANSWERED", asked.json()
    assert {c["version_id"] for c in asked.json()["citations"]} == {version_id}

    # 未授权导师 → 403
    world["mentor"] = _token(world["tenant_a"], world["mentor_principal"],
                             scopes=("mentor",), allowed=("internal_training",),
                             subject=world["mentor_subject"])
    denied = api.post("/v1/training/practical-signoffs",
                      json={"assignment_id": assignment_id, "decision": "PASS"},
                      headers=_h(world["mentor"]))
    assert denied.status_code == 403, denied.text

    with psycopg.connect(world['dsn']) as owner_conn:
        owner_conn.execute("SELECT set_config('app.tenant_id', %s, false)", (world["tenant_a"],))
        owner_conn.execute(
            """INSERT INTO training_mentor_scopes (tenant_id, mentor_subject_id, cohort_id)
               VALUES (%s, %s, %s)""",
            (world["tenant_a"], world["mentor_subject"], world["cohort"]),
        )
        owner_conn.commit()

    signed = api.post("/v1/training/practical-signoffs",
                      json={"assignment_id": assignment_id, "decision": "PASS"},
                      headers=_h(world["mentor"]))
    assert signed.status_code == 200, signed.text
    assert signed.json()["assignment_status"] == "COMPLETED"


def test_cross_tenant_denied_under_business_role(
    api: TestClient, world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("fkh.api.main.get_generator",
                        lambda *a, **k: _Scripted("内部内容不应对其他租户可见。"))
    body = {
        "filename": "t.md", "title": "内部知识", "domain": "quality",
        "owner_department": "qc", "classification": 1, "audience": "internal",
        "required_scope": "internal_product", "acl_allow_roles": ["staff"],
        "knowledge_key": f"x-{uuid.uuid4().hex[:6]}",
        "content_base64": base64.b64encode("内部内容".encode()).decode(),
    }
    uploaded = api.post("/v1/knowledge/documents", json=body, headers=_h(world["ops"]))
    assert uploaded.status_code == 201, uploaded.text
    version_id = uploaded.json()["version_id"]
    api.post(f"/v1/knowledge/{version_id}/review", json={"decision": "PASS"},
             headers=_h(world["reviewer"]))
    api.post(f"/v1/knowledge/{version_id}/publish", json={"audience": "internal"},
             headers=_h(world["ops"]))

    # 跨租户：用别的租户令牌做写操作与读操作
    denied_write = api.post(f"/v1/knowledge/{version_id}/review", json={"decision": "PASS"},
                            headers=_h(world["outsider"]))
    assert denied_write.status_code in {403, 404}, denied_write.text

    denied_read = api.post(
        "/v1/knowledge/answer",
        json={"query": "内部知识讲了什么？", "knowledge_scope": "internal_product",
              "language": "zh-CN", "max_evidence": 3},
        headers=_h(world["outsider"]),
    )
    assert denied_read.status_code in {200, 403, 404}, denied_read.text
    if denied_read.status_code == 200:
        assert "内部内容" not in denied_read.json().get("answer", "")
        assert denied_read.json()["status"] != "ANSWERED"
