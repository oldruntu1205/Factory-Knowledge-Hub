"""金标准回归（可执行子集）—— 固定数据集 + 固定行为断言。

对应 Rev2 §8.3：`case_id / question / persona / scope / expected_behavior /
forbidden_claims / severity / dataset_version`。这里跑的是**脚本层**（确定性），
模型层 RAG Triad 打分等 ADR-08 校准后再上（阈值未定，不写死百分比）。

设计取舍：
- 一个租户、一次播种，所有用例共用同一批知识，避免"每个用例造一套数据"导致漂移；
- 模型用脚本替身，只验证闸门/可见性/引用行为，不评估生成质量（那是 Triad 的事）。
"""

from __future__ import annotations

import base64
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import jwt
import psycopg
import pytest
import yaml
from fastapi.testclient import TestClient
from tests.conftest import create_principal, create_tenant

from fkh.api.main import create_app
from fkh.config import Settings

pytestmark = pytest.mark.requires_pg

SECRET = "golden-secret-0123456789abcdef"
HASH_KEY = "golden-hmac-0123456789"
HASH_KEY_ID = "k-golden"
CASES = Path(__file__).with_name("cases.yaml")

PUBLIC_CATALOG = (
    "第 1 页\n\n货号 规格 管径*总长 上标口*下标口 单价\n"
    "A112420 200mm 38*300 24*24 39.00\n"
)
INTERNAL_SOP = (
    "内部成本与工艺（不得对外）\n\n"
    "A112420 的出厂成本为 21.50 元，毛利目标 45%。\n"
)
QC_DOC = (
    "冷凝管检验规范\n\n"
    "气密性检验：充气至 0.2MPa，保压 60 秒，压力下降不超过 5% 为合格。\n"
)
TRAINING_DOC = (
    "包装与入库培训\n\n"
    "入库：按货号分区存放，先进先出，湿度保持在 45% 以下。\n"
)


class _Scripted:
    """脚本模型：回答里附带证据 id；分类器答"否"。"""

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
        # 答案必须与证据原文一致（literal-support 会校验），按问题挑对应原文
        if "保压" in prompt:
            answer = "充气至 0.2MPa，保压 60 秒，压力下降不超过 5% 为合格。"
        elif "规格" in prompt:
            answer = "该型号规格为 200mm 38*300 24*24。"
        else:
            answer = self._answer
        return _Generated(text=f"{answer}\n引用：{cite}")


@pytest.fixture(scope="module")
def client(pg_url: str) -> Iterator[TestClient]:
    settings = Settings(
        jwt_secret=SECRET, database_url=pg_url,
        retrieval_hash_key=HASH_KEY, retrieval_hash_key_id=HASH_KEY_ID,
    )
    with TestClient(create_app(settings), raise_server_exceptions=False) as test_client:
        yield test_client


def _token(
    tenant: str, principal: str, *, scopes: tuple[str, ...], allowed: tuple[str, ...],
    roles: tuple[str, ...] = ("staff",), subject: str | None = None,
) -> str:
    now = datetime.now(UTC)
    return jwt.encode(
        {
            "iss": "fkh.local", "aud": "fkh-api", "sub": subject or f"{principal[:8]}@example.test",
            "iat": now, "exp": now + timedelta(minutes=30), "tenant_id": tenant,
            "principal_id": principal, "roles": list(roles), "scope": list(scopes),
            "allowed_scopes": list(allowed),
        },
        SECRET,
        algorithm="HS256",
    )


def _h(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="module")
def world(pg_url: str) -> dict[str, Any]:
    admin = psycopg.connect(pg_url)
    tenant = create_tenant(admin, f"金标准 {uuid.uuid4().hex[:6]}")
    ops = create_principal(admin, tenant, f"ops-{uuid.uuid4().hex[:6]}@example.test")
    reviewer = create_principal(admin, tenant, f"rev-{uuid.uuid4().hex[:6]}@example.test")
    publisher = create_principal(admin, tenant, f"pub-{uuid.uuid4().hex[:6]}@example.test")
    customer = create_principal(admin, tenant, f"buyer-{uuid.uuid4().hex[:6]}@example.test")
    learner = create_principal(admin, tenant, f"stu-{uuid.uuid4().hex[:6]}@example.test")
    admin.commit()
    admin.close()
    return {
        "tenant": tenant,
        "learner_subject": f"stu-{uuid.uuid4().hex[:6]}",
        "ops": _token(tenant, ops,
                      scopes=("knowledge.ingest", "knowledge.read", "knowledge.review",
                              "knowledge.publish", "knowledge.revoke", "knowledge.answer",
                              "knowledge.search", "evidence.read", "training_admin",
                              "training_publisher"),
                      allowed=("internal_product", "customer_public", "internal_training")),
        "reviewer": _token(tenant, reviewer, scopes=("knowledge.review",),
                           allowed=("internal_product", "customer_public", "internal_training")),
        "publisher": _token(tenant, publisher, scopes=("knowledge.publish_public",),
                            allowed=("customer_public",), roles=("public_approver",)),
        "customer": _token(tenant, customer,
                           scopes=("knowledge.answer", "knowledge.search", "evidence.read"),
                           allowed=("customer_public",), roles=("customer",),
                           subject=f"buyer-{uuid.uuid4().hex[:6]}"),
        "learner": _token(tenant, learner, scopes=("learner",), allowed=("internal_training",),
                          subject=f"stu-{uuid.uuid4().hex[:6]}"),
    }


def _upload(
    client: TestClient, world: dict[str, Any], text: str, *,
    domain: str = "quality", classification: int = 1, audience: str = "internal",
    scope: str = "internal_product",
) -> str:
    body = {
        "filename": f"{uuid.uuid4().hex[:6]}.md",
        "title": f"金标准样例 {uuid.uuid4().hex[:4]}",
        "domain": domain,
        "owner_department": "qc",
        "classification": classification,
        "audience": audience,
        "required_scope": scope,
        "acl_allow_roles": ["staff", "customer"],
        "knowledge_key": f"golden-{uuid.uuid4().hex[:8]}",
        "content_base64": base64.b64encode(text.encode()).decode(),
        "catalog_currency": "CNY" if domain == "product_catalog" else None,
    }
    response = client.post("/v1/knowledge/documents", json=body, headers=_h(world["ops"]))
    assert response.status_code == 201, response.text
    return response.json()["version_id"]


def _publish_public(client: TestClient, world: dict[str, Any], version_id: str) -> None:
    assert client.post(f"/v1/knowledge/{version_id}/review", json={"decision": "PASS"},
                       headers=_h(world["reviewer"])).status_code == 200
    assert client.post(f"/v1/knowledge/{version_id}/publish", json={"audience": "internal"},
                       headers=_h(world["ops"])).status_code == 200
    assert client.post(f"/v1/knowledge/{version_id}/publish",
                       json={"audience": "customer_public"},
                       headers=_h(world["publisher"])).status_code == 200


@pytest.fixture(scope="module")
def seeded(
    client: TestClient, world: dict[str, Any], monkeypatch_module: pytest.MonkeyPatch
) -> dict[str, Any]:
    monkeypatch_module.setattr(
        "fkh.api.main.get_generator",
        lambda *a, **k: _Scripted("该型号规格为 200mm 38*300 24*24；保压 60 秒合格。"),
    )
    monkeypatch_module.setattr(
        "fkh.api.answer_exec.get_generator",
        lambda *a, **k: _Scripted("气密性检验保压 60 秒合格。"),
    )
    public = _upload(client, world, PUBLIC_CATALOG, domain="product_catalog",
                     classification=0, audience="customer_public", scope="customer_public")
    _publish_public(client, world, public)
    internal = _upload(client, world, INTERNAL_SOP)
    qc_version = _upload(client, world, QC_DOC)
    training = _upload(client, world, TRAINING_DOC, domain="training",
                       audience="internal", scope="internal_training")
    for version in (internal, qc_version, training):
        client.post(f"/v1/knowledge/{version}/review", json={"decision": "PASS"},
                    headers=_h(world["reviewer"]))
        client.post(f"/v1/knowledge/{version}/publish", json={"audience": "internal"},
                    headers=_h(world["ops"]))

    # 培训课程：把课程 B（包装入库）分配给学员 A 之外的人，用于"跨课程问不出来"
    course = client.post(
        "/v1/training/courses",
        json={"role_code": f"golden-{uuid.uuid4().hex[:4]}", "role_name": "包装工",
              "title": "包装与入库", "knowledge_version_ids": [training]},
        headers=_h(world["ops"]),
    )
    assert course.status_code == 200, course.text
    course_id = course.json()["course_id"]
    client.post(f"/v1/training/courses/{course_id}/questions",
                json={"question_type": "SINGLE_CHOICE", "prompt": "湿度？",
                      "answer_key": {"correct": "45%"}}, headers=_h(world["ops"]))
    published_course = client.post(
        f"/v1/training/courses/{course_id}/publish", headers=_h(world["ops"])
    )
    assert published_course.status_code == 200, published_course.text
    # 学员 A 被分配的是"另一门课"（质检课），因此问不到包装课的 45%
    qc_course = client.post(
        "/v1/training/courses",
        json={"role_code": f"qc-{uuid.uuid4().hex[:4]}", "role_name": "检验员",
              "title": "检验课", "knowledge_version_ids": [qc_version]},
        headers=_h(world["ops"]),
    )
    qc_id = qc_course.json()["course_id"]
    client.post(f"/v1/training/courses/{qc_id}/questions",
                json={"question_type": "SINGLE_CHOICE", "prompt": "保压？",
                      "answer_key": {"correct": "60 秒"}}, headers=_h(world["ops"]))
    published_qc = client.post(
        f"/v1/training/courses/{qc_id}/publish", headers=_h(world["ops"])
    )
    assert published_qc.status_code == 200, published_qc.text
    assignment = client.post("/v1/training/assignments",
                             json={"course_id": qc_id,
                                   "learner_subject_id": world["learner_subject"]},
                             headers=_h(world["ops"]))
    assert assignment.status_code == 200, assignment.text
    return {
        "public_version": public,
        "assignment_id": assignment.json()["assignment_id"],
    }


@pytest.fixture(scope="module")
def monkeypatch_module() -> Iterator[pytest.MonkeyPatch]:
    mp = pytest.MonkeyPatch()
    yield mp
    mp.undo()


def _ask(client: TestClient, world: dict[str, Any], case: dict[str, Any], seeded: dict[str, Any]) -> Any:
    persona = case["persona"]
    if persona == "learner":
        return client.post(
            "/v1/training/ask",
            json={"assignment_id": seeded["assignment_id"], "question": case["question"],
                  "language": "zh-CN"},
            headers=_h(world["learner"]),
        )
    token = world[persona] if persona in {"customer", "ops"} else world["ops"]
    return client.post(
        "/v1/knowledge/answer",
        json={"query": case["question"], "knowledge_scope": case["scope"],
              "language": "zh-CN", "max_evidence": 5},
        headers=_h(token),
    )


def test_golden_cases(
    client: TestClient, world: dict[str, Any], seeded: dict[str, Any]
) -> None:
    dataset = yaml.safe_load(CASES.read_text(encoding="utf-8"))
    assert dataset["dataset_version"], "金标准集必须有 dataset_version"
    failures: list[str] = []

    for case in dataset["cases"]:
        response = _ask(client, world, case, seeded)
        assert response.status_code in {200, 403}, (case["case_id"], response.text)
        if response.status_code == 403:
            body: dict[str, Any] = {"status": "ACCESS_DENIED", "answer": ""}
        else:
            body = response.json()

        status = body.get("status", "")
        expected = case["expected_behavior"]
        answer = body.get("answer", "")

        ok = status != "ANSWERED" if expected == "NOT_ANSWERED" else status == expected
        if not ok:
            failures.append(f"{case['case_id']} 期望 {expected}，实际 {status}")

        for forbidden in case.get("forbidden_claims") or []:
            if forbidden in answer:
                failures.append(f"{case['case_id']} 答案含禁止内容 {forbidden!r}")

        if case.get("severity") == "P0" and status == "ANSWERED" and not body.get("citations"):
            failures.append(f"{case['case_id']} P0 答案没有引用")

    assert not failures, "金标准回归失败：\n" + "\n".join(failures)
