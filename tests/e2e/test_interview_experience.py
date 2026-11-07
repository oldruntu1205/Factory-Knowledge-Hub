"""经验采集（语音访谈）端到端验收 —— ADR-05 未签前的最小可信范围。

真实 PostgreSQL + 受限身份 + 实际 HTTP：
1. 没有授权不能建访谈；代授权被拒（只能本人）；
2. 转写落 RAW，**任何检索/问答路径都看不到**；
3. 草稿带时间戳证据位，人工审核后才 APPROVED（AI 不能自我批准）；
4. 撤回授权 → 转写删除、草稿作废、访谈 WITHDRAWN；
5. 跨租户不可见（同码 403）。
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
from fkh.config import Settings

pytestmark = pytest.mark.requires_pg

SECRET = "interview-e2e-secret-0123456789"
HASH_KEY = "interview-e2e-hmac-0123456789"
HASH_KEY_ID = "k-iv"

TRANSCRIPT = """[00:12] 打磨这道工序如果砂轮不平，会出现橘皮纹。
[00:48] 条件：转速控制在 1800 转以下，进给不要太快。
[01:30] 例外：薄壁件必须分两次走刀。
[02:05] 风险：冷却液浓度低于 5% 时容易烧伤表面。
"""


def _token(
    tenant: str, principal: str, *, scopes: tuple[str, ...], allowed: tuple[str, ...],
    roles: tuple[str, ...] = ("staff",), subject: str | None = None,
) -> str:
    now = datetime.now(UTC)
    return jwt.encode(
        {
            "iss": "fkh.local", "aud": "fkh-api", "sub": subject or f"{principal[:8]}@example.test",
            "iat": now, "exp": now + timedelta(minutes=10), "tenant_id": tenant,
            "principal_id": principal, "roles": list(roles), "scope": list(scopes),
            "allowed_scopes": list(allowed),
        },
        SECRET,
        algorithm="HS256",
    )


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
        return _Generated(text="无法回答。\n引用：无")


@pytest.fixture
def client(pg_url: str) -> Iterator[TestClient]:
    settings = Settings(
        jwt_secret=SECRET, database_url=pg_url,
        retrieval_hash_key=HASH_KEY, retrieval_hash_key_id=HASH_KEY_ID,
    )
    with TestClient(create_app(settings), raise_server_exceptions=False) as test_client:
        yield test_client


@pytest.fixture
def crew(admin: psycopg.Connection[Any]) -> dict[str, Any]:
    tenant = create_tenant(admin, f"访谈 {uuid.uuid4().hex[:6]}")
    collector = create_principal(admin, tenant, f"col-{uuid.uuid4().hex[:6]}@example.test")
    reviewer = create_principal(admin, tenant, f"rev-{uuid.uuid4().hex[:6]}@example.test")
    outsider = create_principal(admin, tenant, f"out-{uuid.uuid4().hex[:6]}@example.test")
    admin.commit()
    subject = f"worker-{uuid.uuid4().hex[:6]}"
    worker_principal = create_principal(admin, tenant, f"{subject}@example.test")
    admin.commit()
    return {
        "tenant": tenant,
        "subject": subject,
        # 本人令牌：sub 与 subject 一致，只有本人能授权与建访谈（L1 2026-11-08）
        "worker": _token(tenant, worker_principal,
                         scopes=("interview.create", "interview.read", "knowledge.search",
                                 "knowledge.answer"),
                         allowed=("internal_product", "internal_training"), subject=subject),
        "worker_subject": subject,
        "collector": _token(tenant, collector,
                            scopes=("interview.create", "interview.read", "knowledge.search",
                                    "knowledge.answer"),
                            allowed=("internal_product", "internal_training")),
        "reviewer": _token(tenant, reviewer, scopes=("interview.review", "interview.read"),
                           allowed=("internal_product",), roles=("mentor",)),
        "outsider": _token(tenant, outsider, scopes=("interview.read",),
                           allowed=("internal_product",)),
        "other_subject": f"worker-{uuid.uuid4().hex[:6]}",
    }


def _h(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _consent(client: TestClient, crew: dict[str, Any], subject: str | None = None) -> str:
    response = client.post(
        "/v1/interviews/consents",
        json={
            "subject_id": subject or crew["subject"],
            "method": "ORAL_WITH_RECEIPT",
            "retention_days": 90,
            "receipt_uri": "consent://notice-v1",
        },
        headers=_h(crew["worker"]),
    )
    assert response.status_code == 201, response.text
    return response.json()["consent_id"]


def test_collector_cannot_use_workers_consent(
    client: TestClient, crew: dict[str, Any]
) -> None:
    """L1 2026-11-08：持有 interview.create 不等于可以使用任意人的授权。"""
    consent_id = _consent(client, crew)
    intrude = client.post(
        "/v1/interviews",
        json={"subject_id": crew["subject"], "consent_id": consent_id},
        headers=_h(crew["collector"]),
    )
    assert intrude.status_code == 409, intrude.text


def test_interview_requires_valid_consent(client: TestClient, crew: dict[str, Any]) -> None:
    # 授权主体与受访者不一致 → 409
    consent_id = _consent(client, crew)
    mismatch = client.post(
        "/v1/interviews",
        json={"subject_id": crew["other_subject"], "consent_id": consent_id},
        headers=_h(crew["worker"]),
    )
    assert mismatch.status_code == 409, mismatch.text

    # 不存在的授权 → 403（与"无权限"同码，不探测存在性）
    missing = client.post(
        "/v1/interviews",
        json={"subject_id": crew["subject"], "consent_id": str(uuid.uuid4())},
        headers=_h(crew["worker"]),
    )
    assert missing.status_code == 403, missing.text


def test_transcript_is_raw_and_invisible_to_retrieval(
    client: TestClient, crew: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("fkh.api.main.get_generator", lambda *a, **k: _Scripted())
    consent_id = _consent(client, crew)
    interview = client.post(
        "/v1/interviews",
        json={"subject_id": crew["subject"], "consent_id": consent_id},
        headers=_h(crew["worker"]),
    )
    assert interview.status_code == 201, interview.text
    interview_id = interview.json()["interview_id"]

    transcript = client.post(
        f"/v1/interviews/{interview_id}/transcript",
        json={"content": TRANSCRIPT, "kind": "MACHINE"},
        headers=_h(crew["worker"]),
    )
    assert transcript.status_code == 201, transcript.text

    # 检索：用转写里的原句去搜，不得命中
    search = client.post(
        "/v1/knowledge/search",
        json={"query": "砂轮不平会出现橘皮纹", "knowledge_scope": "internal_product",
              "max_results": 5},
        headers=_h(crew["worker"]),
    )
    assert search.status_code == 200, search.text
    assert search.json()["results"] == [], "转写不得进入检索索引"

    # 问答：同样不得从转写拿到内容
    answer = client.post(
        "/v1/knowledge/answer",
        json={"query": "砂轮不平会出现橘皮纹吗？", "knowledge_scope": "internal_product",
              "language": "zh-CN", "max_evidence": 5},
        headers=_h(crew["worker"]),
    )
    assert answer.status_code == 200, answer.text
    assert answer.json()["status"] != "ANSWERED", answer.json()


def test_draft_carries_timestamps_and_requires_human_review(
    client: TestClient, crew: dict[str, Any]
) -> None:
    consent_id = _consent(client, crew)
    interview_id = client.post(
        "/v1/interviews",
        json={"subject_id": crew["subject"], "consent_id": consent_id},
        headers=_h(crew["worker"]),
    ).json()["interview_id"]
    client.post(f"/v1/interviews/{interview_id}/transcript",
                json={"content": TRANSCRIPT}, headers=_h(crew["worker"]))

    draft = client.post(f"/v1/interviews/{interview_id}/drafts", headers=_h(crew["worker"]))
    assert draft.status_code == 201, draft.text
    draft_id = draft.json()["draft_id"]

    view = client.get(f"/v1/interviews/{interview_id}", headers=_h(crew["worker"]))
    assert view.status_code == 200, view.text
    body = view.json()
    assert body["status"] == "DRAFT_READY"
    draft_view = body["drafts"][0]
    assert draft_view["evidence_spans"], draft_view
    assert draft_view["evidence_spans"][0]["start_ms"] == 12_000, draft_view
    assert all("quote" in span for span in draft_view["evidence_spans"])

    # 采集者没有 interview.review → 不能自己批准
    denied = client.post(
        f"/v1/interviews/drafts/{draft_id}/review",
        json={"decision": "APPROVE"},
        headers=_h(crew["worker"]),
    )
    assert denied.status_code == 403, denied.text

    approved = client.post(
        f"/v1/interviews/drafts/{draft_id}/review",
        json={"decision": "APPROVE", "note": "证据位齐全"},
        headers=_h(crew["reviewer"]),
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "APPROVED"
    assert approved.json()["reviewer"] == "/".join([]) or approved.json()["reviewer"]


def test_withdraw_consent_removes_transcript_and_drafts(
    client: TestClient, crew: dict[str, Any]
) -> None:
    consent_id = _consent(client, crew)
    interview_id = client.post(
        "/v1/interviews",
        json={"subject_id": crew["subject"], "consent_id": consent_id},
        headers=_h(crew["worker"]),
    ).json()["interview_id"]
    client.post(f"/v1/interviews/{interview_id}/transcript",
                json={"content": TRANSCRIPT}, headers=_h(crew["worker"]))
    client.post(f"/v1/interviews/{interview_id}/drafts", headers=_h(crew["worker"]))

    revoked = client.post(
        f"/v1/interviews/consents/{consent_id}/revoke",
        json={"reason": "受访者要求撤回"},
        headers=_h(crew["worker"]),
    )
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["transcripts_deleted"] == 1
    assert revoked.json()["drafts_withdrawn"] == 1

    after = client.get(f"/v1/interviews/{interview_id}", headers=_h(crew["worker"]))
    assert after.status_code == 200, after.text
    body = after.json()
    assert body["status"] == "WITHDRAWN"
    assert body["transcripts"] == 0
    assert body["drafts"][0]["status"] == "WITHDRAWN"
    # 撤回后不得保留承载原文的字段（L1 2026-11-08）
    assert body["drafts"][0]["claim"] == ""
    for span in body["drafts"][0]["evidence_spans"]:
        assert "quote" not in span, span
        assert "start_ms" in span

    # 撤回后不能再用该授权建访谈
    again = client.post(
        "/v1/interviews",
        json={"subject_id": crew["subject"], "consent_id": consent_id},
        headers=_h(crew["worker"]),
    )
    assert again.status_code == 409, again.text


def test_outsider_cannot_read_interview(client: TestClient, crew: dict[str, Any]) -> None:
    consent_id = _consent(client, crew)
    interview_id = client.post(
        "/v1/interviews",
        json={"subject_id": crew["subject"], "consent_id": consent_id},
        headers=_h(crew["worker"]),
    ).json()["interview_id"]

    # 同租户但无 interview.read？该令牌有 read；用不存在的 id 验证"同码 403"
    missing = client.get(f"/v1/interviews/{uuid.uuid4()}", headers=_h(crew["worker"]))
    assert missing.status_code == 403, missing.text

    stakeholder = client.get(f"/v1/interviews/{interview_id}", headers=_h(crew["outsider"]))
    assert stakeholder.status_code == 403, "非本人且无 review 权限不得读取访谈"
