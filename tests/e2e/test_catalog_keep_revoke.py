"""KEEP 决议 + 撤销 + 清理收敛的真实 PostgreSQL 回归（2026-11-08）。

要验证的三件事：
1. **撤销后旧引用立即拒绝**（不依赖清理是否完成）；
2. **清理能完成**（KEEP 会写 catalog_items、候选会被外键牵连——0029 之前这里直接失败）；
3. **人工决议仍可审计**（候选被级联删除后，`resolved_snapshot` 仍保留规格/价格/原文/操作者）。

全部走真实 PostgreSQL；HTTP 验收用非 owner、无 BYPASSRLS 的业务角色。
"""

from __future__ import annotations

import json
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
from fkh.catalog.conflicts import resolve_group
from fkh.config import Settings
from fkh.workers.revoke_purge import consume_revoke_events

pytestmark = pytest.mark.requires_pg

SECRET = "keep-e2e-secret-0123456789ab"
HASH_KEY = "keep-e2e-hmac-0123456789"
HASH_KEY_ID = "k-keep"

# 块里同时出现两行同货号（模拟重抽取口径变化），KEEP 选 39.50 那一行；
# 目录通道要求"结构化事实能在原文块里字面找到"，所以两行都必须在这段原文里。
CATALOG = (
    "第 1 页\n\n货号 规格 管径*总长 上标口*下标口 单价\n"
    "A112420 200mm 38*300 24*24 39.00\n"
    "A112420 200mm 38*300 24*24 39.50\n"
    "A112430 300mm 38*400 24*24 48.00\n"
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
        import re as _re

        found = _re.search(r'<evidence id="([0-9a-f-]{36})"', prompt)
        cite = found.group(1) if found else "无"
        return _Generated(text=f"该型号规格为 200mm 38*300 24*24。\n引用：{cite}")


def _token(tenant: str, principal: str, *, scopes: tuple[str, ...], allowed: tuple[str, ...],
           roles: tuple[str, ...] = ("staff",)) -> str:
    now = datetime.now(UTC)
    return jwt.encode(
        {
            "iss": "fkh.local", "aud": "fkh-api", "sub": f"{principal[:8]}@keep.test",
            "iat": now, "exp": now + timedelta(minutes=20), "tenant_id": tenant,
            "principal_id": principal, "roles": list(roles), "scope": list(scopes),
            "allowed_scopes": list(allowed),
        },
        SECRET,
        algorithm="HS256",
    )


@pytest.fixture(scope="module")
def role(pg_url: str) -> Iterator[str]:
    name, dsn = ensure_business_role(pg_url, prefix="fkh_keep")
    yield dsn
    drop_business_role(pg_url, name)


@pytest.fixture
def client(role: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setattr("fkh.api.main.get_generator", lambda *a, **k: _Scripted())
    settings = Settings(jwt_secret=SECRET, database_url=role,
                        retrieval_hash_key=HASH_KEY, retrieval_hash_key_id=HASH_KEY_ID)
    with TestClient(create_app(settings), raise_server_exceptions=False) as test_client:
        yield test_client


@pytest.fixture
def world(client: TestClient, admin: psycopg.Connection[Any]) -> dict[str, Any]:
    tenant = create_tenant(admin, f"KEEP {uuid.uuid4().hex[:6]}")
    ops = create_principal(admin, tenant, f"ops-{uuid.uuid4().hex[:6]}@keep.test")
    approver = create_principal(admin, tenant, f"pub-{uuid.uuid4().hex[:6]}@keep.test")
    admin.commit()
    token = _token(str(tenant), str(ops),
                   scopes=("knowledge.ingest", "knowledge.read", "knowledge.review",
                           "knowledge.publish", "knowledge.revoke", "knowledge.answer",
                           "knowledge.search", "evidence.read"),
                   allowed=("internal_product", "customer_public"))
    return {"tenant": tenant, "ops": ops, "approver": approver, "token": token}


def _h(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _seed_conflict(admin: psycopg.Connection[Any], world: dict[str, Any],
                   client: TestClient) -> dict[str, Any]:
    """造一个"抽取歧义"版本的 KEEP 决议场景。

    直接写库（不走 ingest 抽取）：版本 PUBLISHED_PUBLIC + 一个父/子块 + 冲突组与候选，
    然后用领域服务 `resolve_group` 做 KEEP —— 它把候选回填成 catalog_items。
    """
    import hashlib
    import json

    from fkh.workers.index_maintenance import mark_generation_ready, switch_visible_generation

    version_id = uuid.uuid4()
    document_id = uuid.uuid4()
    parent_id = uuid.uuid4()
    child_id = uuid.uuid4()
    acl_id = uuid.uuid4()
    group_id = uuid.uuid4()
    keep_candidate = uuid.uuid4()
    # 公开审批人必须与技术审批人不同（库内 check）

    with psycopg.connect(admin.info.dsn) as conn:
        conn.execute("SELECT set_config('app.tenant_id', %s, false)", (str(world["tenant"]),))
        conn.execute(
            "INSERT INTO acl_policies (tenant_id, id, policy_json) VALUES (%s, %s, %s)",
            (world["tenant"], acl_id, json.dumps({"allow": {"roles": ["staff", "customer"]},
                                                  "deny": {}})),
        )
        conn.execute(
            "INSERT INTO knowledge_documents (tenant_id, id, knowledge_key, domain, owner_department)"
            " VALUES (%s, %s, %s, 'product_catalog', 'sales')",
            (world["tenant"], document_id, f"keep-{uuid.uuid4().hex[:8]}"),
        )
        conn.execute(
            """INSERT INTO knowledge_versions
                 (tenant_id, id, document_id, version_no, title, classification, audience,
                  status, content_sha256, source_object_key, required_scope,
                  approved_by, public_approved_by)
               VALUES (%s, %s, %s, 1, 'KEEP 目录', 0, 'customer_public', 'PUBLISHED_PUBLIC',
                       %s, 'keep/catalog.md', 'customer_public', %s, %s)""",
            (world["tenant"], version_id, document_id,
             hashlib.sha256(b"keep").hexdigest(), world["ops"], world["approver"]),
        )
        conn.execute(
            """INSERT INTO knowledge_chunks
                 (tenant_id, id, version_id, chunk_kind, chunk_text, chunk_sha256,
                  tokenizer_id, locator, acl_policy_id, index_generation)
               VALUES (%s, %s, %s, 'parent', %s, %s, 'tok-v1', %s, %s, 1)""",
            (world["tenant"], parent_id, version_id, CATALOG,
             hashlib.sha256(CATALOG.encode()).hexdigest(),
             json.dumps({"block_index": 0}), acl_id),
        )
        conn.execute(
            """INSERT INTO knowledge_chunks
                 (tenant_id, id, version_id, parent_chunk_id, chunk_kind, chunk_text,
                  chunk_sha256, tokenizer_id, locator, acl_policy_id, index_generation)
               VALUES (%s, %s, %s, %s, 'child', %s, %s, 'tok-v1', %s, %s, 1)""",
            (world["tenant"], child_id, version_id, parent_id,
             "A112420 200mm 38*300 24*24 39.50",
             hashlib.sha256(b"child").hexdigest(),
             json.dumps({"block_index": 0, "paragraph_index": 0}), acl_id),
        )
        conn.execute(
            "INSERT INTO catalog_conflict_groups (tenant_id, group_id, version_id, code)"
            " VALUES (%s, %s, %s, 'A112420')",
            (world["tenant"], group_id, version_id),
        )
        conn.execute(
            """INSERT INTO catalog_conflict_candidates
                 (tenant_id, group_id, candidate_id, chunk_id, raw_text, spec, raw_price, price)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
            (world["tenant"], group_id, keep_candidate, parent_id,
             "A112420 200mm 38*300 24*24 39.50", "200mm 38*300 24*24", "39.50", 39.50),
        )
        mark_generation_ready(conn, world["tenant"], 1, 2)
        switch_visible_generation(conn, world["tenant"], 1)
        conn.commit()

    with psycopg.connect(admin.info.dsn) as conn:
        conn.execute("SELECT set_config('app.tenant_id', %s, false)", (str(world["tenant"]),))
        resolved = resolve_group(
            conn, world["tenant"], version_id=version_id, code="A112420",
            actor_id=world["ops"], candidate_id=keep_candidate, note="按最新抽取口径保留",
        )
        conn.commit()
        snapshot = conn.execute(
            "SELECT status, selected_candidate_id, resolved_snapshot"
            " FROM catalog_conflict_groups WHERE tenant_id = %s AND group_id = %s",
            (world["tenant"], group_id),
        ).fetchone()

    assert resolved.status == "KEEP", resolved
    assert snapshot[0] == "KEEP"
    assert snapshot[2] is not None, "决议必须写快照"
    return {"version_id": version_id, "group_id": group_id,
            "candidate_id": keep_candidate, "chunk_id": parent_id}


def test_keep_resolution_survives_revoke_and_purge(
    client: TestClient, world: dict[str, Any], admin: psycopg.Connection[Any]
) -> None:
    seeded = _seed_conflict(admin, world, client)

    # 1) KEEP 生效：能答（内部视角可含价格），引用指向该版本
    answered = client.post(
        "/v1/knowledge/answer",
        json={"query": "A112420 的规格是什么？", "knowledge_scope": "customer_public",
              "language": "zh-CN", "max_evidence": 5},
        headers=_h(world["token"]),
    )
    assert answered.status_code == 200, answered.text
    body = answered.json()
    assert body["status"] == "ANSWERED", body
    assert "39.50" in body["answer"], body
    citation = body["citations"][0]
    assert citation["version_id"] == str(seeded["version_id"])
    evidence_id = citation["evidence_id"]
    assert client.get(f"/v1/evidence/{evidence_id}",
                      headers=_h(world["token"])).status_code == 200

    # 2) 撤销 → 旧引用立即拒绝
    revoked = client.post(
        f"/v1/knowledge/{seeded['version_id']}/revoke?reason=keep-drill",
        headers=_h(world["token"]),
    )
    assert revoked.status_code == 200, revoked.text
    assert client.get(f"/v1/evidence/{evidence_id}",
                      headers=_h(world["token"])).status_code in {403, 404}

    # 3) 清理能完成（KEEP 写过 catalog_items；候选被外键牵连 → 0029 级联）
    with psycopg.connect(admin.info.dsn) as conn:
        conn.execute("SELECT set_config('app.tenant_id', %s, false)", (str(world["tenant"]),))
        kept_items = conn.execute(
            "SELECT count(*) FROM catalog_items WHERE tenant_id = %s AND version_id = %s",
            (world["tenant"], seeded["version_id"]),
        ).fetchone()[0]
        assert kept_items == 1, "KEEP 应回填恰好一条目录条目（撤销前）"

        result = consume_revoke_events(conn, world["tenant"])
        if result.processed < 1:
            rows = conn.execute(
                "SELECT status, attempts, last_error FROM outbox_deliveries"
                " WHERE tenant_id = %s",
                (world["tenant"],),
            ).fetchall()
            raise AssertionError((result, rows))

        chunks = conn.execute(
            "SELECT count(*) FROM knowledge_chunks WHERE tenant_id = %s AND version_id = %s",
            (world["tenant"], seeded["version_id"]),
        ).fetchone()[0]
        items = conn.execute(
            "SELECT count(*) FROM catalog_items WHERE tenant_id = %s AND version_id = %s",
            (world["tenant"], seeded["version_id"]),
        ).fetchone()[0]
        candidates = conn.execute(
            "SELECT count(*) FROM catalog_conflict_candidates"
            " WHERE tenant_id = %s AND group_id = %s",
            (world["tenant"], seeded["group_id"]),
        ).fetchone()[0]
        assert chunks == 0 and items == 0 and candidates == 0

        # 4) 人工决议仍可审计：组保留 KEEP，候选为空，但快照完整
        group = conn.execute(
            "SELECT status, selected_candidate_id, resolved_by, resolved_at, note,"
            "       resolved_snapshot"
            "  FROM catalog_conflict_groups WHERE tenant_id = %s AND group_id = %s",
            (world["tenant"], seeded["group_id"]),
        ).fetchone()
        assert group is not None, "冲突组必须保留（审计事实）"
        status, selected, resolved_by, resolved_at, note, snapshot = group
        assert status == "KEEP"
        assert selected is None, "候选被清理后 selected_candidate_id 应被置空"
        assert str(resolved_by) == str(world["ops"]) and resolved_at is not None
        assert note == "按最新抽取口径保留"
        assert snapshot is not None, "当选候选被清理后必须保留快照"
        assert str(snapshot["candidate_id"]) == str(seeded["candidate_id"])
        assert snapshot["spec"] == "200mm 38*300 24*24"
        assert snapshot["raw_price"] == "39.50"
        assert snapshot["decision"] == "KEEP"
        assert str(snapshot["resolved_by"]) == str(world["ops"])
        assert snapshot["raw_text"].startswith("A112420")


def test_keep_requires_structurally_valid_snapshot(
    client: TestClient, world: dict[str, Any], admin: psycopg.Connection[Any]
) -> None:
    """L1 2026-11-08：KEEP 的库层约束必须挡住结构无效的快照（`{}`/缺键/空值）。"""
    seeded = _seed_conflict(admin, world, client)

    with psycopg.connect(admin.info.dsn) as conn:
        conn.execute("SELECT set_config('app.tenant_id', %s, false)", (str(world["tenant"]),))
        # 空对象：应被 0034 的结构约束拒绝
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                "UPDATE catalog_conflict_groups SET resolved_snapshot = '{}'::jsonb"
                " WHERE tenant_id = %s AND group_id = %s",
                (world["tenant"], seeded["group_id"]),
            )
        conn.rollback()

    with psycopg.connect(admin.info.dsn) as conn:
        conn.execute("SELECT set_config('app.tenant_id', %s, false)", (str(world["tenant"]),))
        # 缺 spec / raw_price 的快照同样拒绝
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                "UPDATE catalog_conflict_groups SET resolved_snapshot = %s::jsonb"
                " WHERE tenant_id = %s AND group_id = %s",
                (
                    json.dumps({"decision": "KEEP", "candidate_id": "x", "code": "A112420",
                                "resolved_by": "y"}),
                    world["tenant"],
                    seeded["group_id"],
                ),
            )
        conn.rollback()
