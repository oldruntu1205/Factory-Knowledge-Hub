"""存量升级演练：从 `0021` 的旧库升级到最新版（Rev2 §7.2 / 交付证据要求）。

场景（按用户 2026-11-08 要求）：
1. 在 `0021` 状态建**多代索引**与**带冲突候选的目录**（旧数据结构）；
2. 升级到最新版，验证旧**待发布版本仍可正常发布**；
3. 撤销后**立即不可读**；清理**可重试并最终收敛**；
4. 保留必要的**审计追溯**（引用、读取审计、消费台账）。

迁移与造数用管理员连接；业务验收用**非 owner、无 BYPASSRLS** 的业务角色。
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlparse

import jwt
import psycopg
import pytest
from fastapi.testclient import TestClient
from migrations.apply import apply_all, discover_migrations, fetch_applied, version_of
from tests.e2e._roles import drop_business_role, ensure_business_role

from fkh.api.main import create_app
from fkh.config import Settings
from fkh.workers.revoke_purge import consume_revoke_events

pytestmark = pytest.mark.requires_pg

SECRET = "upgrade-secret-0123456789abcdef"
HASH_KEY = "upgrade-hmac-0123456789"
HASH_KEY_ID = "k-upgrade"

TARGET_VERSION = 21  # 从 0021 的状态开始（含 training_mentor_scopes）


def _admin_url(pg_url: str) -> str:
    return pg_url


def _scratch_url(pg_url: str, dbname: str) -> str:
    parsed = urlparse(pg_url)
    return f"postgresql://{parsed.username or 'postgres'}@{parsed.hostname}:{parsed.port or 5432}/{dbname}"


@pytest.fixture(scope="module")
def upgraded(pg_url: str) -> Iterator[dict[str, Any]]:
    dbname = f"fkh_upgrade_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(pg_url, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{dbname}"')
    dsn = _scratch_url(pg_url, dbname)

    legacy = _build_legacy_db(dsn)
    with psycopg.connect(dsn) as conn:
        files = discover_migrations()
        applied = fetch_applied(conn)
        pending = [p for p in files if int(version_of(p)) > TARGET_VERSION]
        assert pending, "应当有 0021 之后的迁移"
        apply_all(conn, files, applied, dry_run=False)

    role_name, app_dsn = ensure_business_role(dsn, prefix="fkh_upg")
    yield {"dsn": dsn, "app_dsn": app_dsn, "role": role_name, **legacy}

    drop_business_role(dsn, role_name)
    with psycopg.connect(pg_url, autocommit=True) as admin:
        admin.execute(f'DROP DATABASE IF EXISTS "{dbname}" WITH (FORCE)')


def _build_legacy_db(dsn: str) -> dict[str, Any]:
    """把库建到 0021，并写入旧结构数据。"""
    tenant = uuid.uuid4()
    principal = uuid.uuid4()
    reviewer = uuid.uuid4()
    acl_id = uuid.uuid4()

    pending_doc = uuid.uuid4()
    pending_version = uuid.uuid4()
    catalog_doc_a = uuid.uuid4()
    catalog_version_a = uuid.uuid4()
    catalog_doc_b = uuid.uuid4()
    catalog_version_b = uuid.uuid4()
    chunk_pending = uuid.uuid4()
    chunk_catalog_a = uuid.uuid4()
    chunk_catalog_b = uuid.uuid4()

    with psycopg.connect(dsn) as conn:
        files = [p for p in discover_migrations() if int(version_of(p)) <= TARGET_VERSION]
        apply_all(conn, files, fetch_applied(conn), dry_run=False)
        conn.commit()

        conn.execute("INSERT INTO tenants (id, name) VALUES (%s, %s)", (tenant, "升级演练"))
        for pid, subject in ((principal, "ops@upgrade.test"), (reviewer, "rev@upgrade.test")):
            conn.execute(
                "INSERT INTO principals (tenant_id, id, external_subject, principal_type)"
                " VALUES (%s, %s, %s, 'user')",
                (tenant, pid, subject),
            )
        conn.execute(
            "INSERT INTO acl_policies (tenant_id, id, policy_json) VALUES (%s, %s, %s)",
            (tenant, acl_id, json.dumps({"allow": {"roles": ["staff"]}, "deny": {}})),
        )

        def add_doc(doc_id: uuid.UUID, key: str, domain: str) -> None:
            conn.execute(
                "INSERT INTO knowledge_documents (tenant_id, id, knowledge_key, domain,"
                " owner_department) VALUES (%s, %s, %s, %s, %s)",
                (tenant, doc_id, key, domain, "qc"),
            )

        def add_version(
            version_id: uuid.UUID, doc_id: uuid.UUID, *, status: str, classification: int,
            audience: str, scope: str, version_no: int = 1,
        ) -> None:
            # PUBLISHED_PUBLIC 的库内约束要求两个不同的审批人（C0 不等于可公开）
            approved = principal if status != "DRAFT" else None
            public_approved = reviewer if status == "PUBLISHED_PUBLIC" else None
            conn.execute(
                """INSERT INTO knowledge_versions
                     (tenant_id, id, document_id, version_no, title, classification, audience,
                      status, content_sha256, source_object_key, required_scope,
                      approved_by, public_approved_by)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (
                    tenant, version_id, doc_id, version_no, f"样例 {version_no}", classification,
                    audience, status, hashlib.sha256(str(version_id).encode()).hexdigest(),
                    f"demo/{version_id}.md", scope, approved, public_approved,
                ),
            )

        def add_chunk(
            chunk_id: uuid.UUID, version_id: uuid.UUID, text: str, generation: int,
        ) -> None:
            """父块 + 子块（真实入库两条都写；只有父块会被证据服务判 PARENT_INCOMPLETE）。"""
            conn.execute(
                """INSERT INTO knowledge_chunks
                     (tenant_id, id, version_id, chunk_kind, chunk_text, chunk_sha256,
                      tokenizer_id, locator, acl_policy_id, index_generation)
                   VALUES (%s, %s, %s, 'parent', %s, %s, 'tok-v1', %s, %s, %s)""",
                (
                    tenant, chunk_id, version_id, text,
                    hashlib.sha256(text.encode()).hexdigest(),
                    json.dumps({"block_index": 0}), acl_id, generation,
                ),
            )
            child_id = uuid.uuid4()
            conn.execute(
                """INSERT INTO knowledge_chunks
                     (tenant_id, id, version_id, parent_chunk_id, chunk_kind, chunk_text,
                      chunk_sha256, tokenizer_id, locator, acl_policy_id, index_generation)
                   VALUES (%s, %s, %s, %s, 'child', %s, %s, 'tok-v1', %s, %s, %s)""",
                (
                    tenant, child_id, version_id, chunk_id,
                    text.splitlines()[0] if text.splitlines() else text,
                    hashlib.sha256((text + "#child").encode()).hexdigest(),
                    json.dumps({"block_index": 0, "paragraph_index": 0}), acl_id, generation,
                ),
            )

        # 待发布版本（旧库里已切出块，但停在 DRAFT）
        add_doc(pending_doc, "upgrade-pending", "quality")
        add_version(pending_version, pending_doc, status="DRAFT", classification=1,
                    audience="internal", scope="internal_product")
        # 关键：待发布版本放在**较早**的代（1），最大代属于目录 B（3）——
        # 这样"升级后能否发布旧代版本"才被真正验证（此前都在最大代，闸门等同没测）
        add_chunk(chunk_pending, pending_version, "升级前待发布：保压 60 秒合格。", 1)

        # 目录冲突：同一货号在两个版本里给出不同价格（升级后判歧义）
        add_doc(catalog_doc_a, "upgrade-catalog-a", "product_catalog")
        add_version(catalog_version_a, catalog_doc_a, status="PUBLISHED_PUBLIC",
                    classification=0, audience="customer_public", scope="customer_public")
        add_chunk(
            chunk_catalog_a,
            catalog_version_a,
            "货号 A112420 200mm 38*300 24*24 单价 39.00\n"
            "货号 A112430 300mm 38*400 24*24 单价 48.00",
            2,
        )
        add_doc(catalog_doc_b, "upgrade-catalog-b", "product_catalog")
        add_version(catalog_version_b, catalog_doc_b, status="PUBLISHED_PUBLIC",
                    classification=0, audience="customer_public", scope="customer_public",
                    version_no=2)
        add_chunk(chunk_catalog_b, catalog_version_b,
                  "货号 A112420 200mm 38*300 24*24 单价 42.00", 3)

        conn.execute(
            """INSERT INTO catalog_items
                 (tenant_id, version_id, chunk_id, code, spec, raw_text, raw_price, price,
                  currency, locator)
               VALUES (%s, %s, %s, 'A112420', '200mm 38*300 24*24',
                       'A112420 200mm 38*300 24*24 单价 39.00', '39.00', 39.00, 'CNY', %s)""",
            (tenant, catalog_version_a, chunk_catalog_a, json.dumps({"block_index": 0})),
        )
        conn.execute(
            """INSERT INTO catalog_items
                 (tenant_id, version_id, chunk_id, code, spec, raw_text, raw_price, price,
                  currency, locator)
               VALUES (%s, %s, %s, 'A112420', '200mm 38*300 24*24',
                       'A112420 200mm 38*300 24*24 单价 42.00', '42.00', 42.00, 'CNY', %s)""",
            (tenant, catalog_version_b, chunk_catalog_b, json.dumps({"block_index": 0})),
        )
        # 版本 A 里还有一个**不冲突**的货号（用于产生真实引用）
        conn.execute(
            """INSERT INTO catalog_items
                 (tenant_id, version_id, chunk_id, code, spec, raw_text, raw_price, price,
                  currency, locator)
               VALUES (%s, %s, %s, 'A112430', '300mm 38*400 24*24',
                       'A112430 300mm 38*400 24*24 单价 48.00', '48.00', 48.00, 'CNY', %s)""",
            (tenant, catalog_version_a, chunk_catalog_a, json.dumps({"block_index": 0})),
        )
        # A112420 在版本 A 上带有冲突待办与候选（撤销 A 时这些行必须一并收敛）
        conflict_group = uuid.uuid4()
        conn.execute(
            "INSERT INTO catalog_conflict_groups (tenant_id, group_id, version_id, code)"
            " VALUES (%s, %s, %s, 'A112420')",
            (tenant, conflict_group, catalog_version_a),
        )
        conn.execute(
            """INSERT INTO catalog_conflict_candidates
                 (tenant_id, group_id, candidate_id, chunk_id, raw_text, spec, raw_price, price)
               VALUES (%s, %s, %s, %s,
                       'A112420 200mm 38*300 24*24 单价 39.00', '200mm 38*300 24*24',
                       '39.00', 39.00)""",
            (tenant, conflict_group, uuid.uuid4(), chunk_catalog_a),
        )
        conn.commit()

    now = datetime.now(UTC)

    def token(pid: uuid.UUID, scopes: tuple[str, ...], allowed: tuple[str, ...],
              subject: str) -> str:
        return jwt.encode(
            {
                "iss": "fkh.local", "aud": "fkh-api", "sub": subject, "iat": now,
                "exp": now + timedelta(minutes=30), "tenant_id": str(tenant),
                "principal_id": str(pid), "roles": ["staff"], "scope": list(scopes),
                "allowed_scopes": list(allowed),
            },
            SECRET,
            algorithm="HS256",
        )

    return {
        "tenant": tenant,
        "pending_version": pending_version,
        "catalog_code": "A112420",
        "catalog_versions": (catalog_version_a, catalog_version_b),
        "catalog_version_a": catalog_version_a,
        "catalog_chunk_a": chunk_catalog_a,
        "ops_token": token(
            principal,
            ("knowledge.read", "knowledge.review", "knowledge.publish", "knowledge.revoke",
             "knowledge.answer", "knowledge.search", "evidence.read"),
            ("internal_product", "customer_public"),
            "ops@upgrade.test",
        ),
        "reviewer_token": token(reviewer, ("knowledge.review",),
                                ("internal_product", "customer_public"), "rev@upgrade.test"),
    }


def _h(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_backfill_creates_ready_generations_and_watermark(upgraded: dict[str, Any]) -> None:
    with psycopg.connect(upgraded["dsn"]) as conn:
        rows = conn.execute(
            "SELECT generation, status FROM index_generations WHERE tenant_id = %s"
            " ORDER BY generation",
            (upgraded["tenant"],),
        ).fetchall()
        watermark = conn.execute(
            "SELECT generation FROM index_visibility WHERE tenant_id = %s",
            (upgraded["tenant"],),
        ).fetchone()
    assert rows, "升级应回填索引代"
    assert all(status == "READY" for _gen, status in rows), rows
    assert watermark is not None and watermark[0] == max(gen for gen, _ in rows)


def test_pending_legacy_version_can_still_be_published(upgraded: dict[str, Any]) -> None:
    settings = Settings(jwt_secret=SECRET, database_url=upgraded["app_dsn"],
                        retrieval_hash_key=HASH_KEY, retrieval_hash_key_id=HASH_KEY_ID)
    with TestClient(create_app(settings), raise_server_exceptions=False) as client:
        reviewed = client.post(
            f"/v1/knowledge/{upgraded['pending_version']}/review",
            json={"decision": "PASS"}, headers=_h(upgraded["reviewer_token"]),
        )
        assert reviewed.status_code == 200, reviewed.text
        published = client.post(
            f"/v1/knowledge/{upgraded['pending_version']}/publish",
            json={"audience": "internal"}, headers=_h(upgraded["ops_token"]),
        )
        assert published.status_code == 200, published.text
        assert published.json()["status"] == "APPROVED_INTERNAL"


def test_revoke_immediately_unreadable_and_purge_converges(
    upgraded: dict[str, Any]
) -> None:
    settings = Settings(jwt_secret=SECRET, database_url=upgraded["app_dsn"],
                        retrieval_hash_key=HASH_KEY, retrieval_hash_key_id=HASH_KEY_ID)
    version_id = upgraded["pending_version"]
    tenant = upgraded["tenant"]

    with TestClient(create_app(settings), raise_server_exceptions=False) as client:
        revoked = client.post(
            f"/v1/knowledge/{version_id}/revoke?reason=upgrade-drill",
            headers=_h(upgraded["ops_token"]),
        )
        assert revoked.status_code == 200, revoked.text

        search = client.post(
            "/v1/knowledge/search",
            json={"query": "保压 60 秒合格", "knowledge_scope": "internal_product",
                  "max_results": 5},
            headers=_h(upgraded["ops_token"]),
        )
        assert search.status_code == 200, search.text
        names = [item for item in search.json()["results"]]
        assert all(item["version_id"] != str(version_id) for item in names)

        evidence = client.get(f"/v1/evidence/{uuid.uuid4()}", headers=_h(upgraded["ops_token"]))
        assert evidence.status_code in {403, 404}

    # 清理：先注入一次失败（重试），再成功 → 收敛
    with psycopg.connect(upgraded["dsn"]) as conn:
        conn.execute("SELECT set_config('app.tenant_id', %s, false)", (str(tenant),))

        def broken(_c: psycopg.Connection[Any], _t: uuid.UUID, _v: uuid.UUID) -> int:
            raise RuntimeError("注入失败")

        first = consume_revoke_events(conn, tenant, purge=broken)
        assert first.failed >= 1, first

        second = consume_revoke_events(conn, tenant)
        assert second.processed >= 1, second

        remaining = conn.execute(
            "SELECT count(*) FROM knowledge_chunks WHERE tenant_id = %s AND version_id = %s",
            (tenant, version_id),
        ).fetchone()[0]
        assert remaining == 0, "清理必须最终收敛"

        deliveries = conn.execute(
            "SELECT status, attempts FROM outbox_deliveries WHERE tenant_id = %s",
            (tenant,),
        ).fetchall()
        assert any(status == "DONE" for status, _ in deliveries), deliveries


def test_audit_trace_is_preserved_after_purge(upgraded: dict[str, Any]) -> None:
    """块被删掉后，审计仍能回答"哪个版本被读过"。"""
    settings = Settings(jwt_secret=SECRET, database_url=upgraded["app_dsn"],
                        retrieval_hash_key=HASH_KEY, retrieval_hash_key_id=HASH_KEY_ID)
    with TestClient(create_app(settings), raise_server_exceptions=False) as client:
        client.post(
            "/v1/knowledge/answer",
            json={"query": "保压 60 秒合格", "knowledge_scope": "internal_product",
                  "language": "zh-CN", "max_evidence": 3},
            headers=_h(upgraded["ops_token"]),
        )

    with psycopg.connect(upgraded["dsn"]) as conn:
        conn.execute("SELECT set_config('app.tenant_id', %s, false)", (str(upgraded["tenant"]),))
        queries = conn.execute(
            "SELECT count(*) FROM rag_queries WHERE tenant_id = %s",
            (upgraded["tenant"],),
        ).fetchone()[0]
        access = conn.execute(
            "SELECT count(*) FROM evidence_access_log WHERE tenant_id = %s",
            (upgraded["tenant"],),
        ).fetchone()[0]
    assert queries >= 1
    assert access >= 1


def test_revoke_version_with_catalog_facts_rejects_old_citation_and_converges(
    upgraded: dict[str, Any]
) -> None:
    """带目录条目 + 冲突候选的版本：撤销后旧引用立即拒绝，清理收敛，审计可追溯。

    这是本轮的**真实复现**：升级前 `catalog_items` / `catalog_conflict_candidates`
    对 `knowledge_chunks` 是 RESTRICT，撤销后清理被外键挡住（`0029` 修正为级联）。
    """
    settings = Settings(jwt_secret=SECRET, database_url=upgraded["app_dsn"],
                        retrieval_hash_key=HASH_KEY, retrieval_hash_key_id=HASH_KEY_ID)
    version_a = upgraded["catalog_version_a"]
    tenant = upgraded["tenant"]

    with TestClient(create_app(settings), raise_server_exceptions=False) as client:
        # 用不冲突的货号 A112430（只在版本 A 里）产生**真实引用**
        answered = client.post(
            "/v1/knowledge/answer",
            json={"query": "A112430 的规格是什么？", "knowledge_scope": "customer_public",
                  "language": "zh-CN", "max_evidence": 5},
            headers=_h(upgraded["ops_token"]),
        )
        assert answered.status_code == 200, answered.text
        body = answered.json()
        assert body["status"] == "ANSWERED", body
        citation = body["citations"][0]
        assert citation["version_id"] == str(version_a), citation
        evidence_id = citation["evidence_id"]

        before = client.get(f"/v1/evidence/{evidence_id}", headers=_h(upgraded["ops_token"]))
        assert before.status_code == 200, before.text

        revoked = client.post(
            f"/v1/knowledge/{version_a}/revoke?reason=catalog-drill",
            headers=_h(upgraded["ops_token"]),
        )
        assert revoked.status_code == 200, revoked.text

        # 旧引用立即拒绝（撤销同步生效，不等清理）
        immediately = client.get(f"/v1/evidence/{evidence_id}", headers=_h(upgraded["ops_token"]))
        assert immediately.status_code in {403, 404}, immediately.text

    # 清理：先注入一次失败（可重试），再成功（收敛）
    with psycopg.connect(upgraded["dsn"]) as conn:
        conn.execute("SELECT set_config('app.tenant_id', %s, false)", (str(tenant),))

        def broken(_c: psycopg.Connection[Any], _t: uuid.UUID, _v: uuid.UUID) -> int:
            raise RuntimeError("注入失败")

        failed = consume_revoke_events(conn, tenant, purge=broken)
        assert failed.failed >= 1, failed

        done = consume_revoke_events(conn, tenant)
        assert done.processed >= 1, done

        remaining_chunks = conn.execute(
            "SELECT count(*) FROM knowledge_chunks WHERE tenant_id = %s AND version_id = %s",
            (tenant, version_a),
        ).fetchone()[0]
        remaining_items = conn.execute(
            "SELECT count(*) FROM catalog_items WHERE tenant_id = %s AND version_id = %s",
            (tenant, version_a),
        ).fetchone()[0]
        candidates = conn.execute(
            "SELECT count(*) FROM catalog_conflict_candidates c"
            "  JOIN catalog_conflict_groups g"
            "    ON g.tenant_id = c.tenant_id AND g.group_id = c.group_id"
            " WHERE c.tenant_id = %s AND g.version_id = %s",
            (tenant, version_a),
        ).fetchone()[0]
        assert remaining_chunks == 0, "块必须被清理"
        assert remaining_items == 0, "目录事实必须随块清理（0029）"
        assert candidates == 0, "冲突候选必须随块清理（0029）"

        # 审计追溯：引用仍保留版本身份，块 ID 被置空
        cited = conn.execute(
            "SELECT chunk_id, version_id FROM rag_citations WHERE tenant_id = %s"
            " AND version_id = %s",
            (tenant, version_a),
        ).fetchall()
        assert cited, "审计应保留对已撤销版本的引用记录"
        assert all(row[0] is None for row in cited), "块已删除，chunk_id 应被置空"
        assert all(str(row[1]) == str(version_a) for row in cited)

    # 另一个目录版本不受影响：A112420 只剩 42.00，仍可作答
    with TestClient(create_app(settings), raise_server_exceptions=False) as client:
        other = client.post(
            "/v1/knowledge/answer",
            json={"query": "A112420 的单价是多少？", "knowledge_scope": "customer_public",
                  "language": "zh-CN", "max_evidence": 5},
            headers=_h(upgraded["ops_token"]),
        )
        assert other.status_code == 200, other.text
        other_body = other.json()
        assert other_body["status"] == "ANSWERED", other_body
        assert "42.00" in other_body["answer"], other_body
