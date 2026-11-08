"""访谈授权撤回的权限与并发验收（2026-11-08 修复）。

基线 `1b1701d` 的问题：
- `revoke_consent` **不校验调用者是否为授权本人**——同租户任何持 `interview.create` 的人
  都能撤回他人的授权；
- 重复撤回静默通过（没有明确的 409）；
- 撤回与转写/审核/晋升之间没有行锁，理论上并发写入可能在撤回提交后落库。

本文件用真实 PostgreSQL + **非 owner、无 BYPASSRLS 的业务角色** + 实际 HTTP 验收权限部分，
并用两条真实连接验收并发部分。
"""

from __future__ import annotations

import threading
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
from fkh.domain.interview import InterviewError, InterviewService

pytestmark = pytest.mark.requires_pg

SECRET = "revoke-e2e-secret-0123456789ab"
HASH_KEY = "revoke-e2e-hmac-0123456789"
HASH_KEY_ID = "k-revoke"

TRANSCRIPT = (
    "[00:10] 打磨砂轮不平会出现橘皮纹。\n"
    "[00:40] 条件：转速控制在 1800 转以下。\n"
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
    name, dsn = ensure_business_role(pg_url, prefix="fkh_revoke")
    yield dsn
    drop_business_role(pg_url, name)


@pytest.fixture
def client(role: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setattr("fkh.api.main.get_generator", lambda *a, **k: _Scripted())
    settings = Settings(jwt_secret=SECRET, database_url=role,
                        retrieval_hash_key=HASH_KEY, retrieval_hash_key_id=HASH_KEY_ID)
    with TestClient(create_app(settings), raise_server_exceptions=__import__('os').getenv('FKH_RAISE')=='1') as test_client:
        yield test_client


@pytest.fixture
def crew(admin: psycopg.Connection[Any]) -> dict[str, Any]:
    tenant = create_tenant(admin, f"撤回 {uuid.uuid4().hex[:6]}")
    worker = create_principal(admin, tenant, f"w-{uuid.uuid4().hex[:6]}@example.test")
    other = create_principal(admin, tenant, f"o-{uuid.uuid4().hex[:6]}@example.test")
    reviewer = create_principal(admin, tenant, f"r-{uuid.uuid4().hex[:6]}@example.test")
    admin.commit()
    subject = f"worker-{uuid.uuid4().hex[:6]}"
    return {
        "tenant": tenant,
        "subject": subject,
        "worker": _token(tenant, worker, scopes=("interview.create", "interview.read",
                                                 "knowledge.search", "knowledge.answer"),
                         allowed=("internal_product",), subject=subject),
        "other": _token(tenant, other, scopes=("interview.create", "interview.read"),
                        allowed=("internal_product",), subject=f"other-{uuid.uuid4().hex[:6]}"),
        "reviewer": _token(tenant, reviewer,
                           scopes=("interview.review", "interview.read", "knowledge.write"),
                           allowed=("internal_product",), subject=f"rev-{uuid.uuid4().hex[:6]}"),
    }


def _h(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _consent(client: TestClient, crew: dict[str, Any]) -> str:
    response = client.post(
        "/v1/interviews/consents",
        json={"subject_id": crew["subject"], "method": "WRITTEN", "retention_days": 30},
        headers=_h(crew["worker"]),
    )
    assert response.status_code == 201, response.text
    return response.json()["consent_id"]


def _interview(client: TestClient, crew: dict[str, Any], consent_id: str) -> str:
    response = client.post(
        "/v1/interviews",
        json={"subject_id": crew["subject"], "consent_id": consent_id},
        headers=_h(crew["worker"]),
    )
    assert response.status_code == 201, response.text
    return response.json()["interview_id"]


def test_only_consent_owner_can_revoke(client: TestClient, crew: dict[str, Any]) -> None:
    consent_id = _consent(client, crew)
    intrude = client.post(
        f"/v1/interviews/consents/{consent_id}/revoke",
        json={"reason": "不是本人"},
        headers=_h(crew["other"]),
    )
    assert intrude.status_code == 403, intrude.text

    # 本人仍可正常撤回
    own = client.post(f"/v1/interviews/consents/{consent_id}/revoke", json={},
                      headers=_h(crew["worker"]))
    assert own.status_code == 200, own.text


def test_revoke_before_any_interview(client: TestClient, crew: dict[str, Any]) -> None:
    consent_id = _consent(client, crew)
    revoked = client.post(f"/v1/interviews/consents/{consent_id}/revoke", json={},
                          headers=_h(crew["worker"]))
    assert revoked.status_code == 200, revoked.text
    body = revoked.json()
    assert body["interviews"] == 0
    assert body["transcripts_deleted"] == 0
    assert body["drafts_withdrawn"] == 0


def test_repeat_revoke_is_rejected(client: TestClient, crew: dict[str, Any]) -> None:
    consent_id = _consent(client, crew)
    assert client.post(f"/v1/interviews/consents/{consent_id}/revoke", json={},
                       headers=_h(crew["worker"])).status_code == 200
    again = client.post(f"/v1/interviews/consents/{consent_id}/revoke", json={},
                        headers=_h(crew["worker"]))
    assert again.status_code == 409, again.text


def test_writes_and_promotion_blocked_after_revoke(
    client: TestClient, crew: dict[str, Any]
) -> None:
    consent_id = _consent(client, crew)
    interview_id = _interview(client, crew, consent_id)
    client.post(f"/v1/interviews/{interview_id}/transcript",
                json={"content": TRANSCRIPT, "kind": "HUMAN_EDITED"}, headers=_h(crew["worker"]))
    draft_id = client.post(f"/v1/interviews/{interview_id}/drafts",
                           headers=_h(crew["worker"])).json()["draft_id"]
    client.post(f"/v1/interviews/drafts/{draft_id}/review", json={"decision": "APPROVE"},
                headers=_h(crew["reviewer"]))
    promoted = client.post(
        f"/v1/interviews/drafts/{draft_id}/promote",
        json={"domain": "owner_experience", "required_scope": "internal_product"},
        headers=_h(crew["reviewer"]),
    )
    assert promoted.status_code == 201, promoted.text

    revoked = client.post(f"/v1/interviews/consents/{consent_id}/revoke",
                          json={"reason": "本人要求"}, headers=_h(crew["worker"]))
    assert revoked.status_code == 200, revoked.text

    transcript = client.post(f"/v1/interviews/{interview_id}/transcript",
                             json={"content": TRANSCRIPT}, headers=_h(crew["worker"]))
    assert transcript.status_code == 409, transcript.text

    draft_again = client.post(f"/v1/interviews/{interview_id}/drafts",
                              headers=_h(crew["worker"]))
    assert draft_again.status_code == 409, draft_again.text

    review_again = client.post(f"/v1/interviews/drafts/{draft_id}/review",
                               json={"decision": "APPROVE"}, headers=_h(crew["reviewer"]))
    assert review_again.status_code == 409, review_again.text

    promote_again = client.post(
        f"/v1/interviews/drafts/{draft_id}/promote",
        json={"domain": "owner_experience", "required_scope": "internal_product"},
        headers=_h(crew["reviewer"]),
    )
    assert promote_again.status_code == 409, promote_again.text

    # 已晋升内容撤回后立即不可检索
    search = client.post(
        "/v1/knowledge/search",
        json={"query": "打磨砂轮不平会出现橘皮纹", "knowledge_scope": "internal_product",
              "max_results": 5},
        headers=_h(crew["worker"]),
    )
    assert search.status_code == 200, search.text
    assert search.json()["results"] == []


def test_concurrent_write_blocks_until_revoke_commits(
    admin: psycopg.Connection[Any], crew: dict[str, Any]
) -> None:
    """并发：撤回事务持锁期间，写入必须等待；撤回提交后写入失败且不落库。"""
    dsn = admin.info.dsn
    with psycopg.connect(dsn) as setup:
        setup.execute("SELECT set_config('app.tenant_id', %s, false)", (str(crew["tenant"]),))
        service = InterviewService(setup, crew["tenant"])
        consent_id = service.grant_consent(
            subject_id=crew["subject"], method="WRITTEN", retention_days=30,
            authenticated_subject=crew["subject"],
        )
        interview_id = service.create_interview(
            subject_id=crew["subject"], consent_id=consent_id,
            interviewer=crew["subject"], language="zh-CN",
        )
        setup.commit()

    errors: list[BaseException] = []
    finished = threading.Event()

    def writer() -> None:
        try:
            with psycopg.connect(dsn) as conn:
                conn.execute("SELECT set_config('app.tenant_id', %s, false)",
                             (str(crew["tenant"]),))
                InterviewService(conn, crew["tenant"]).add_transcript(
                    interview_id, content=TRANSCRIPT, actor=crew["subject"]
                )
                conn.commit()
        except BaseException as exc:  # noqa: BLE001 - 测试要看到失败原因
            errors.append(exc)
        finally:
            finished.set()

    with psycopg.connect(dsn) as revoker:
        revoker.execute("SELECT set_config('app.tenant_id', %s, false)", (str(crew["tenant"]),))
        revoke_service = InterviewService(revoker, crew["tenant"])
        # 撤回在**未提交**的事务里执行完（已持行锁、已置 revoked_at）
        revoke_service.revoke_consent(
            consent_id, actor_id=uuid.uuid4(), actor_subject=crew["subject"], reason="并发撤回"
        )

        thread = threading.Thread(target=writer)
        thread.start()
        # 写入方应被未提交的撤回事务阻塞
        assert not finished.wait(1.0), "写入方未等待撤回事务的锁"

        revoker.commit()

    thread.join(timeout=10)
    assert finished.is_set(), "撤回提交后写入方应结束"
    assert errors, "撤回提交后写入必须失败"
    assert isinstance(errors[0], InterviewError), errors[0]

    with psycopg.connect(dsn) as check:
        check.execute("SELECT set_config('app.tenant_id', %s, false)", (str(crew["tenant"]),))
        count = check.execute(
            "SELECT count(*) FROM interview_transcripts WHERE tenant_id = %s AND interview_id = %s",
            (crew["tenant"], interview_id),
        ).fetchone()[0]
    assert count == 0, "撤回提交后不得留下并发写入的转写"
