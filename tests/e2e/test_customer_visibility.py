"""对客可见性验收（Rev2 §3.2 / §3.4 / §4.4 / §4.5）。

审计发现（基准 `7e9681a`）：目录精确查询接受 `APPROVED_INTERNAL`，
合成 C0 目录在**独立公开审批之前**就能让客户拿到单价与引用。

本文件用真实 PostgreSQL + 四个受限身份跑实际 HTTP 接口：
- customer：只有 customer_public 数据范围，无审核/发布权限
- reviewer：技术审核人
- publisher：独立公开审批人（四眼原则的第二人）
- admin：内部运营（上传/审核/内部发布）

验收点：
1. 技术审批后，客户仍读不到（目录精确问答 / 普通检索 / 旧引用直读三路都堵）
2. 公开发布后，客户能读到**非价格事实**（规格等）
3. 客户询价 / 交期 / 质量承诺 / 定制可行性 → `HUMAN_REVIEW_REQUIRED`，且答案里不出现价格
4. 撤销后立即读不到（三路同时失效）
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

SECRET = "customer-visibility-secret-0123456789"
HASH_KEY = "customer-visibility-hmac-0123"
HASH_KEY_ID = "k-cv"

CATALOG = (
    "第 1 页\n\n货号 规格 管径*总长 上标口*下标口 单价\n"
    "A112420 200mm 38*300 24*24 39.00\n"
    "A112430 300mm 38*400 24*24 48.00\n"
)
CATALOG_CODE = "A112420"
CATALOG_PRICE = "39.00"


def _token(
    tenant: str,
    principal: str,
    *,
    scopes: tuple[str, ...],
    allowed: tuple[str, ...],
    roles: tuple[str, ...] = ("staff",),
) -> str:
    now = datetime.now(UTC)
    payload = {
        "iss": "fkh.local",
        "aud": "fkh-api",
        "sub": f"{principal[:8]}@example.test",
        "iat": now,
        "exp": now + timedelta(minutes=10),
        "tenant_id": tenant,
        "principal_id": principal,
        "roles": list(roles),
        "scope": list(scopes),
        "allowed_scopes": list(allowed),
    }
    return jwt.encode(payload, SECRET, algorithm="HS256")


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
def actors(admin: psycopg.Connection[Any]) -> dict[str, str]:
    tenant = create_tenant(admin, f"对客 {uuid.uuid4().hex[:6]}")
    internal = create_principal(admin, tenant, f"ops-{uuid.uuid4().hex[:6]}@example.test")
    reviewer = create_principal(admin, tenant, f"rev-{uuid.uuid4().hex[:6]}@example.test")
    publisher = create_principal(admin, tenant, f"pub-{uuid.uuid4().hex[:6]}@example.test")
    customer = create_principal(admin, tenant, f"cus-{uuid.uuid4().hex[:6]}@example.test")
    admin.commit()
    return {
        "tenant": tenant,
        "internal": internal,
        "reviewer": reviewer,
        "publisher": publisher,
        "customer": customer,
        "internal_token": _token(
            tenant,
            internal,
            scopes=(
                "knowledge.ingest",
                "knowledge.read",
                "knowledge.review",
                "knowledge.publish",
                "knowledge.answer",
                "knowledge.search",
                "evidence.read",
                "catalog.read",
                "knowledge.revoke",
            ),
            allowed=("internal_product", "customer_public"),
        ),
        "reviewer_token": _token(
            tenant, reviewer, scopes=("knowledge.review", "knowledge.read"),
            allowed=("internal_product", "customer_public"),
        ),
        "publisher_token": _token(
            tenant, publisher, scopes=("knowledge.publish_public", "knowledge.read"),
            allowed=("customer_public",), roles=("public_approver",),
        ),
        "customer_token": _token(
            tenant, customer, scopes=("knowledge.answer", "knowledge.search", "evidence.read"),
            allowed=("customer_public",), roles=("customer",),
        ),
    }


def _h(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _upload_catalog(client: TestClient, actors: dict[str, str]) -> str:
    body = {
        "filename": "catalog-c0.md",
        "title": "对外目录（C0 合成）",
        "domain": "product_catalog",
        "owner_department": "sales",
        "classification": 0,
        "audience": "customer_public",
        "required_scope": "customer_public",
        "acl_allow_roles": ["customer", "staff"],
        "acl_deny_roles": [],
        "knowledge_key": f"catalog-c0-{uuid.uuid4().hex[:6]}",
        "content_base64": base64.b64encode(CATALOG.encode()).decode(),
        "catalog_currency": "CNY",
    }
    response = client.post(
        "/v1/knowledge/documents", json=body, headers=_h(actors["internal_token"])
    )
    assert response.status_code == 201, response.text
    return response.json()["version_id"]


def _approve(client: TestClient, version_id: str, actors: dict[str, str]) -> None:
    pass_review = client.post(
        f"/v1/knowledge/{version_id}/review",
        json={"decision": "PASS"},
        headers=_h(actors["reviewer_token"]),
    )
    assert pass_review.status_code == 200, pass_review.text
    internal = client.post(
        f"/v1/knowledge/{version_id}/publish",
        json={"audience": "internal"},
        headers=_h(actors["internal_token"]),
    )
    assert internal.status_code == 200, internal.text


def _publish_public(client: TestClient, version_id: str, actors: dict[str, str]) -> None:
    published = client.post(
        f"/v1/knowledge/{version_id}/publish",
        json={"audience": "customer_public"},
        headers=_h(actors["publisher_token"]),
    )
    assert published.status_code == 200, published.text
    assert published.json()["status"] == "PUBLISHED_PUBLIC"


class _Scripted:
    """脚本化模型：分类器问"是/否"时固定回答，用来验证语义兜底。"""

    model_id = "scripted"

    def __init__(self, verdict: str, answer: str | None = None) -> None:
        self._verdict = verdict
        self._answer = answer

    def generate(self, prompt: str, *, max_tokens: int = 512, temperature: float = 0.0) -> Any:
        from dataclasses import dataclass as _dc

        @_dc
        class _Generated:
            text: str
            model_id: str = "scripted"

        if "意图分类器" in prompt:
            return _Generated(text=self._verdict)
        if self._answer is not None:
            import re as _re

            found = _re.search(r'<evidence id="([0-9a-f-]{36})"', prompt)
            cite = found.group(1) if found else "无"
            return _Generated(text=f"{self._answer}\n引用：{cite}")
        return _Generated(text="无法回答。\n引用：无")


def _last_route(pg_url: str, tenant: str) -> tuple[str | None, str | None]:
    with psycopg.connect(pg_url) as conn:
        conn.execute("SELECT set_config('app.tenant_id', %s, false)", (tenant,))
        row = conn.execute(
            "SELECT route, route_reason FROM rag_queries WHERE tenant_id = %s"
            " ORDER BY created_at DESC LIMIT 1",
            (tenant,),
        ).fetchone()
    return (row[0], row[1]) if row else (None, None)


def _ask(client: TestClient, token: str, query: str) -> Any:
    return client.post(
        "/v1/knowledge/answer",
        json={"query": query, "knowledge_scope": "customer_public", "language": "zh-CN",
              "max_evidence": 5},
        headers=_h(token),
    )


def _search(client: TestClient, token: str, query: str) -> Any:
    return client.post(
        "/v1/knowledge/search",
        json={"query": query, "knowledge_scope": "customer_public", "max_results": 5},
        headers=_h(token),
    )


def test_customer_cannot_read_before_public_approval(
    client: TestClient, actors: dict[str, str], pg_url: str
) -> None:
    """P0：技术审批 + 内部发布之后、独立公开审批之前，客户三路都必须读不到。"""
    version_id = _upload_catalog(client, actors)
    _approve(client, version_id, actors)

    exact = _ask(client, actors["customer_token"], f"{CATALOG_CODE} 的单价是多少？")
    body = exact.json()
    assert exact.status_code == 200, exact.text
    assert body["status"] != "ANSWERED", body
    assert CATALOG_PRICE not in body.get("answer", ""), body

    search = _search(client, actors["customer_token"], CATALOG_CODE)
    assert search.status_code == 200
    versions = {hit["version_id"] for hit in search.json()["results"]}
    assert version_id not in versions, "未公开发布的版本不得进入客户检索结果"

    # 直接拿该版本的块 id 走证据接口：必须 403（不存在/不可见同码）
    with psycopg.connect(pg_url) as conn:
        conn.execute("SELECT set_config('app.tenant_id', %s, false)", (actors["tenant"],))
        chunk_id = conn.execute(
            "SELECT id FROM knowledge_chunks WHERE version_id = %s AND chunk_kind = 'parent'"
            " ORDER BY id LIMIT 1",
            (version_id,),
        ).fetchone()[0]
    evidence = client.get(f"/v1/evidence/{chunk_id}", headers=_h(actors["customer_token"]))
    assert evidence.status_code == 403, evidence.text


def test_customer_reads_public_facts_after_public_approval(
    client: TestClient, actors: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.e2e.test_customer_visibility import _Scripted as _S

    monkeypatch.setattr(
        "fkh.api.main.get_generator", lambda *a, **k: _S("否", "该型号的规格为 200mm 38*300 24*24。")
    )
    version_id = _upload_catalog(client, actors)
    _approve(client, version_id, actors)
    _publish_public(client, version_id, actors)

    spec = _ask(client, actors["customer_token"], f"{CATALOG_CODE} 的规格是什么？")
    assert spec.status_code == 200, spec.text
    assert spec.json()["status"] == "ANSWERED", spec.json()
    assert "200mm" in spec.json()["answer"], spec.json()
    assert spec.json()["citations"], spec.json()


def test_customer_price_and_commitment_questions_go_to_human(
    client: TestClient, actors: dict[str, str]
) -> None:
    """Rev2 §4.5：外部客服的报价/交期/质量承诺/定制可行性一律转人工，且不得带出价格。"""
    version_id = _upload_catalog(client, actors)
    _approve(client, version_id, actors)
    _publish_public(client, version_id, actors)

    for query in (
        f"{CATALOG_CODE} 的单价是多少？",
        "这款冷凝管多少钱？",
        "下单后多久能交货？",
        "你们能保证三年内不炸裂吗？",
        "能不能按我们的图纸定制？",
    ):
        response = _ask(client, actors["customer_token"], query)
        assert response.status_code == 200, (query, response.text)
        body = response.json()
        assert body["status"] == "HUMAN_REVIEW_REQUIRED", (query, body)
        assert CATALOG_PRICE not in body.get("answer", ""), (query, body)


def test_revoke_takes_effect_immediately_for_customer(
    client: TestClient, actors: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.e2e.test_customer_visibility import _Scripted as _S

    monkeypatch.setattr(
        "fkh.api.main.get_generator", lambda *a, **k: _S("否", "该型号的规格为 200mm 38*300 24*24。")
    )
    version_id = _upload_catalog(client, actors)
    _approve(client, version_id, actors)
    _publish_public(client, version_id, actors)

    before = _ask(client, actors["customer_token"], f"{CATALOG_CODE} 的规格是什么？")
    assert before.json()["status"] == "ANSWERED"
    old_citation = before.json()["citations"][0]["evidence_id"]

    revoked = client.post(
        f"/v1/knowledge/{version_id}/revoke?reason=test",
        headers=_h(actors["internal_token"]),
    )
    assert revoked.status_code == 200, revoked.text

    after = _ask(client, actors["customer_token"], f"{CATALOG_CODE} 的规格是什么？")
    assert after.json()["status"] != "ANSWERED", after.json()
    assert _search(client, actors["customer_token"], CATALOG_CODE).json()["results"] == []
    stale = client.get(f"/v1/evidence/{old_citation}", headers=_h(actors["customer_token"]))
    assert stale.status_code == 403, "撤销后旧引用必须立即失效"


def test_internal_catalog_query_still_works(client: TestClient, actors: dict[str, str]) -> None:
    """内部目录查询继续按原权限工作（不能被对客收紧误伤）。"""
    version_id = _upload_catalog(client, actors)
    _approve(client, version_id, actors)

    response = client.post(
        "/v1/knowledge/answer",
        json={"query": f"{CATALOG_CODE} 的单价是多少？", "knowledge_scope": "customer_public",
              "language": "zh-CN", "max_evidence": 5},
        headers=_h(actors["internal_token"]),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "ANSWERED", body
    assert CATALOG_PRICE in body["answer"], body


def test_catalog_channel_itself_denies_unpublished_version(
    client: TestClient, actors: dict[str, str], pg_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """目录精确通道**自身**必须拒绝未公开版本（L1 指出的证据缺口）。

    刻意选一个不含商务承诺词、但确实走目录精确通道的问法（规格），
    避免"被商务拦截提前转人工"掩盖目录通道的权限错误。
    """
    from tests.e2e.test_customer_visibility import _Scripted as _S

    monkeypatch.setattr(
        "fkh.api.main.get_generator", lambda *a, **k: _S("否")
    )
    version_id = _upload_catalog(client, actors)
    _approve(client, version_id, actors)

    response = _ask(client, actors["customer_token"], f"{CATALOG_CODE} 的规格是什么？")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] != "ANSWERED", body
    assert CATALOG_PRICE not in body.get("answer", ""), body

    route, reason = _last_route(pg_url, actors["tenant"])
    assert route == "catalog_exact", f"必须真的进入目录通道再判定不可见，实际 route={route}"
    assert reason in {"not_found", "ambiguous", "inconsistent"}, reason


def test_semantic_fallback_hands_off_non_keyword_business_question(
    client: TestClient, actors: dict[str, str], pg_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """未命中词表的商务问法由语义识别兜底转人工（L1 §3）。"""
    from tests.e2e.test_customer_visibility import _Scripted as _S

    monkeypatch.setattr("fkh.api.main.get_generator", lambda *a, **k: _S("是"))
    version_id = _upload_catalog(client, actors)
    _approve(client, version_id, actors)
    _publish_public(client, version_id, actors)

    response = _ask(client, actors["customer_token"], "这台设备我什么时候能拿到手？")
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "HUMAN_REVIEW_REQUIRED", response.json()
    route, _ = _last_route(pg_url, actors["tenant"])
    assert route == "handoff", route


def test_unknown_or_empty_scopes_are_not_treated_as_internal(
    client: TestClient, actors: dict[str, str]
) -> None:
    """身份不变量：scope 不能天然证明内部身份，空/未知 scope 一律按外部处理（fail-closed）。"""
    version_id = _upload_catalog(client, actors)
    _approve(client, version_id, actors)

    for allowed in ((), ("bogus_scope",)):
        token = _token(
            actors["tenant"],
            actors["customer"],
            scopes=("knowledge.answer", "knowledge.search"),
            allowed=allowed,
            roles=("staff",),
        )
        response = _ask(client, token, f"{CATALOG_CODE} 的单价是多少？")
        assert response.status_code in {200, 403}, response.text
        if response.status_code == 200:
            body = response.json()
            assert body["status"] != "ANSWERED", body
            assert CATALOG_PRICE not in body.get("answer", ""), body
