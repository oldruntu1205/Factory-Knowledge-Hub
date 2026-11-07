"""索引发布闸门与撤销后重建收敛（Rev2 §7.2 / §8.4）。

真实 PostgreSQL + 实际 HTTP：
- 发布前必须有 READY 的索引代；未就绪 → 409，不允许提前可见；
- 发布动作原子推进"可见代水位"；
- 公开内容只在可见水位之内可检索（水位回退即不可见）；
- 撤销：状态同步生效（API 立即拒绝、旧引用直读 403），物理清理异步；
  重建后块被真正删除、水位前移，撤销内容彻底消失（收敛）。
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
from fkh.workers.index_maintenance import (
    mark_generation_failed,
    rebuild_visible_generation,
    visible_generation,
)

pytestmark = pytest.mark.requires_pg

SECRET = "index-gate-secret-0123456789abc"
HASH_KEY = "index-gate-hmac-0123456789"
HASH_KEY_ID = "k-gate"

PUBLIC_DOC = (
    "第 1 页\n\n货号 规格 管径*总长 上标口*下标口 单价\n"
    "A112420 200mm 38*300 24*24 39.00\n"
)
DOC_A = "冷凝管检验规范\n\n气密性检验：充气至 0.2MPa，保压 60 秒，压力下降不超过 5% 为合格。\n"


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
def org(admin: psycopg.Connection[Any], pg_url: str) -> dict[str, Any]:
    tenant = create_tenant(admin, f"闸门 {uuid.uuid4().hex[:6]}")
    ops = create_principal(admin, tenant, f"ops-{uuid.uuid4().hex[:6]}@example.test")
    reviewer = create_principal(admin, tenant, f"rev-{uuid.uuid4().hex[:6]}@example.test")
    publisher = create_principal(admin, tenant, f"pub-{uuid.uuid4().hex[:6]}@example.test")
    admin.commit()
    now = datetime.now(UTC)

    def tok(principal: str, scopes: tuple[str, ...], allowed: tuple[str, ...],
            roles: tuple[str, ...] = ("staff",)) -> str:
        return jwt.encode(
            {
                "iss": "fkh.local", "aud": "fkh-api", "sub": f"{principal[:8]}@example.test",
                "iat": now, "exp": now + timedelta(minutes=10), "tenant_id": str(tenant),
                "principal_id": str(principal), "roles": list(roles),
                "scope": list(scopes), "allowed_scopes": list(allowed),
            },
            SECRET,
            algorithm="HS256",
        )

    customer = create_principal(admin, tenant, f"buyer-{uuid.uuid4().hex[:6]}@example.test")
    admin.commit()
    return {
        "tenant": tenant,
        "dsn": pg_url,
        "customer_id": str(customer),
        "ops": tok(ops, ("knowledge.ingest", "knowledge.read", "knowledge.review",
                         "knowledge.publish", "knowledge.revoke", "knowledge.answer",
                         "knowledge.search", "evidence.read"),
                   ("internal_product", "customer_public")),
        "reviewer": tok(reviewer, ("knowledge.review",), ("customer_public",)),
        "publisher": tok(publisher, ("knowledge.publish_public",), ("customer_public",),
                         roles=("public_approver",)),
    }


def _h(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _upload(
    client: TestClient, org: dict[str, Any], text: str, *, public: bool = False
) -> str:
    body = {
        "filename": f"{uuid.uuid4().hex[:6]}.md",
        "title": "索引闸门样例",
        "domain": "product_catalog" if public else "quality",
        "owner_department": "qc",
        "classification": 0 if public else 1,
        "audience": "customer_public" if public else "internal",
        "required_scope": "customer_public" if public else "internal_product",
        "acl_allow_roles": ["staff", "customer"],
        "knowledge_key": f"gate-{uuid.uuid4().hex[:8]}",
        "content_base64": base64.b64encode(text.encode()).decode(),
        "catalog_currency": "CNY" if public else None,
    }
    response = client.post("/v1/knowledge/documents", json=body, headers=_h(org["ops"]))
    assert response.status_code == 201, response.text
    return response.json()["version_id"]


def test_publish_requires_ready_generation(
    client: TestClient, org: dict[str, Any]
) -> None:
    version_id = _upload(client, org, PUBLIC_DOC, public=True)
    assert client.post(
        f"/v1/knowledge/{version_id}/review", json={"decision": "PASS"},
        headers=_h(org["reviewer"]),
    ).status_code == 200

    # 上传/列表响应不含索引代，直接从库读（这就是之后要标 FAILED 的那一代）
    with psycopg.connect(_dsn(org)) as conn:
        conn.execute("SELECT set_config('app.tenant_id', %s, false)", (str(org["tenant"]),))
        generation = conn.execute(
            "SELECT max(index_generation) FROM knowledge_chunks"
            " WHERE tenant_id = %s AND version_id = %s",
            (org["tenant"], version_id),
        ).fetchone()[0]
    with psycopg.connect(_dsn(org)) as conn:
        conn.execute("SELECT set_config('app.tenant_id', %s, false)", (str(org["tenant"]),))
        mark_generation_failed(conn, org["tenant"], generation)
        conn.commit()

    blocked = client.post(
        f"/v1/knowledge/{version_id}/publish", json={"audience": "internal"},
        headers=_h(org["ops"]),
    )
    assert blocked.status_code == 409, blocked.text
    assert blocked.json()["error"]["code"] == "VERSION_CONFLICT", blocked.json()

    # 闸门拒绝后版本必须仍停在 REVIEWED（没有被"半发布"）
    listing = client.get("/v1/knowledge/documents", headers=_h(org["ops"])).json()
    items = listing.get("items") or listing.get("documents") or []
    status = next(item["status"] for item in items if item["version_id"] == version_id)
    assert status == "REVIEWED", status


def test_publish_advances_visible_generation(
    client: TestClient, org: dict[str, Any]
) -> None:
    version_id = _upload(client, org, PUBLIC_DOC, public=True)
    client.post(f"/v1/knowledge/{version_id}/review", json={"decision": "PASS"},
                headers=_h(org["reviewer"]))
    published = client.post(
        f"/v1/knowledge/{version_id}/publish", json={"audience": "internal"},
        headers=_h(org["ops"]),
    )
    assert published.status_code == 200, published.text

    with psycopg.connect(_dsn(org)) as conn:
        conn.execute("SELECT set_config('app.tenant_id', %s, false)", (str(org["tenant"]),))
        generation = conn.execute(
            "SELECT max(index_generation) FROM knowledge_chunks"
            " WHERE tenant_id = %s AND version_id = %s",
            (org["tenant"], version_id),
        ).fetchone()[0]
        assert int(generation) >= 1
        assert visible_generation(conn, org["tenant"]) == int(generation)


def test_public_content_invisible_when_watermark_behind(
    client: TestClient, org: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("fkh.api.main.get_generator",
                        lambda *a, **k: _Scripted("规格为 200mm 38*300 24*24。"))
    version_id = _upload(client, org, PUBLIC_DOC, public=True)
    client.post(f"/v1/knowledge/{version_id}/review", json={"decision": "PASS"},
                headers=_h(org["reviewer"]))
    client.post(f"/v1/knowledge/{version_id}/publish", json={"audience": "internal"},
                headers=_h(org["ops"]))
    client.post(f"/v1/knowledge/{version_id}/publish", json={"audience": "customer_public"},
                headers=_h(org["publisher"]))

    customer = jwt.encode(
        {
            "iss": "fkh.local", "aud": "fkh-api", "sub": "buyer@example.test",
            "iat": datetime.now(UTC), "exp": datetime.now(UTC) + timedelta(minutes=10),
            "tenant_id": str(org["tenant"]), "principal_id": _customer_id(org),
            "roles": ["customer"], "scope": ["knowledge.answer", "knowledge.search"],
            "allowed_scopes": ["customer_public"],
        },
        SECRET,
        algorithm="HS256",
    )
    before = client.post("/v1/knowledge/search",
                         json={"query": "A112420 规格", "knowledge_scope": "customer_public",
                               "max_results": 5}, headers=_h(customer))
    assert before.json()["results"], "水位之内应能检索到公开内容"

    with psycopg.connect(_dsn(org)) as conn:
        conn.execute("SELECT set_config('app.tenant_id', %s, false)", (str(org["tenant"]),))
        # 水位最小合法值是 1；"尚未切换"用删除该行表达（检索按 COALESCE(...,0) 处理）
        conn.execute("DELETE FROM index_visibility WHERE tenant_id = %s", (org["tenant"],))
        conn.commit()

    after = client.post("/v1/knowledge/search",
                        json={"query": "A112420 规格", "knowledge_scope": "customer_public",
                              "max_results": 5}, headers=_h(customer))
    assert after.json()["results"] == [], "水位回退后旧 generation 不得继续泄露"


def test_revocation_converges_after_rebuild(
    client: TestClient, org: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "fkh.api.main.get_generator",
        lambda *a, **k: _Scripted("气密性检验的保压时间为 60 秒，压力下降不超过 5% 为合格。"),
    )
    version_id = _upload(client, org, DOC_A)
    client.post(f"/v1/knowledge/{version_id}/review", json={"decision": "PASS"},
                headers=_h(org["reviewer"]))
    assert client.post(f"/v1/knowledge/{version_id}/publish", json={"audience": "internal"},
                       headers=_h(org["ops"])).status_code == 200

    answered = client.post(
        "/v1/knowledge/answer",
        json={"query": "气密性检验的保压时间是多少？", "knowledge_scope": "internal_product",
              "language": "zh-CN", "max_evidence": 5},
        headers=_h(org["ops"]),
    )
    assert answered.json()["status"] == "ANSWERED", answered.json()
    stale_citation = answered.json()["citations"][0]["evidence_id"]

    revoked = client.post(f"/v1/knowledge/{version_id}/revoke?reason=gate-test",
                          headers=_h(org["ops"]))
    assert revoked.status_code == 200, revoked.text

    after_revoke = client.post(
        "/v1/knowledge/answer",
        json={"query": "气密性检验的保压时间是多少？", "knowledge_scope": "internal_product",
              "language": "zh-CN", "max_evidence": 5},
        headers=_h(org["ops"]),
    )
    assert after_revoke.json()["status"] != "ANSWERED"
    assert client.get(f"/v1/evidence/{stale_citation}",
                      headers=_h(org["ops"])).status_code == 403

    # 撤销是同步生效的；块此时仍存在（等异步清理）
    with psycopg.connect(_dsn(org)) as conn:
        conn.execute("SELECT set_config('app.tenant_id', %s, false)", (str(org["tenant"]),))
        before_purge = conn.execute(
            "SELECT count(*) FROM knowledge_chunks WHERE tenant_id = %s AND version_id = %s",
            (org["tenant"], version_id),
        ).fetchone()[0]
    assert before_purge > 0, "撤销不该同步阻塞在物理删除上"

    with psycopg.connect(_dsn(org)) as conn:
        conn.execute("SELECT set_config('app.tenant_id', %s, false)", (str(org["tenant"]),))
        new_generation = rebuild_visible_generation(conn, org["tenant"])
        conn.commit()
        remaining = conn.execute(
            "SELECT count(*) FROM knowledge_chunks WHERE tenant_id = %s AND version_id = %s",
            (org["tenant"], version_id),
        ).fetchone()[0]
        assert visible_generation(conn, org["tenant"]) == new_generation

    assert remaining == 0, "重建后撤销版本的块必须被真正删除（收敛）"
    assert client.get(f"/v1/evidence/{stale_citation}",
                      headers=_h(org["ops"])).status_code == 403


def _dsn(org: dict[str, Any]) -> str:
    return org["dsn"]


def _customer_id(org: dict[str, Any]) -> str:
    return org["customer_id"]


def test_public_evidence_direct_read_respects_watermark(
    client: TestClient, org: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """L1 阻断项 4：水位缺失时，公开内容不仅检索为空，**直读证据也必须拒绝**。"""
    monkeypatch.setattr("fkh.api.main.get_generator",
                        lambda *a, **k: _Scripted("规格为 200mm 38*300 24*24。"))
    version_id = _upload(client, org, PUBLIC_DOC, public=True)
    client.post(f"/v1/knowledge/{version_id}/review", json={"decision": "PASS"},
                headers=_h(org["reviewer"]))
    client.post(f"/v1/knowledge/{version_id}/publish", json={"audience": "internal"},
                headers=_h(org["ops"]))
    client.post(f"/v1/knowledge/{version_id}/publish", json={"audience": "customer_public"},
                headers=_h(org["publisher"]))

    with psycopg.connect(_dsn(org)) as conn:
        conn.execute("SELECT set_config('app.tenant_id', %s, false)", (str(org["tenant"]),))
        chunk_id = conn.execute(
            "SELECT id FROM knowledge_chunks WHERE tenant_id = %s AND version_id = %s"
            " AND chunk_kind = 'parent' LIMIT 1",
            (org["tenant"], version_id),
        ).fetchone()[0]

    ok = client.get(f"/v1/evidence/{chunk_id}", headers=_h(org["ops"]))
    assert ok.status_code == 200, ok.text

    with psycopg.connect(_dsn(org)) as conn:
        conn.execute("SELECT set_config('app.tenant_id', %s, false)", (str(org["tenant"]),))
        conn.execute("DELETE FROM index_visibility WHERE tenant_id = %s", (org["tenant"],))
        conn.commit()

    blocked = client.get(f"/v1/evidence/{chunk_id}", headers=_h(org["ops"]))
    assert blocked.status_code in {403, 404}, blocked.text
