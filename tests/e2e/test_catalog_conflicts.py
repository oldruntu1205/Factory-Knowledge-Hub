"""目录冲突复核队列端到端：记录 → 决议 → 回填 → 幂等 → 身份与并发。

对应 2026-11-06 L1 裁决（`_evidence/l1_reply_catalog_conflicts_20261106.md`）：
完整候选可见、组决议原子生效、身份可信、重抽取尊重人工结论、失效版本不泄漏价格。
"""

from __future__ import annotations

import hashlib
import threading
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import jwt
import psycopg
import pytest
from fastapi.testclient import TestClient
from tests.conftest import create_principal, create_tenant, set_tenant

from fkh.api.main import create_app
from fkh.authz.policy import AclPolicy
from fkh.catalog.conflicts import (
    ConflictError,
    open_summary,
    resolve_group,
)
from fkh.config import Settings
from fkh.domain.review import ReviewService
from fkh.workers.ingestion import IngestionRequest, ingest_document
from fkh.workers.repository import IngestionRepository

pytestmark = pytest.mark.requires_pg

SECRET = "conflict-secret-0123456789abcdef"
HASH_KEY = "conflict-hmac-key-0123456789"
HASH_KEY_ID = "k-conf"

# 两个块里同一货号、不同原文行 → 冲突
LINE_A = "A112420 200mm 38*300 24*24 39.00"
LINE_B = "A112420 300mm 38*400 24*24 48.00"


def _token(tenant_id: str, principal_id: str) -> str:
    now = datetime.now(UTC)
    payload = {
        "iss": "fkh.local",
        "aud": "fkh-api",
        "sub": f"user-{principal_id[:8]}@example.test",
        "iat": now,
        "exp": now + timedelta(minutes=10),
        "tenant_id": tenant_id,
        "principal_id": principal_id,
        "roles": ["staff"],
        "scope": ["knowledge.answer"],
        "allowed_scopes": ["internal_product"],
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


def _chunk(admin: psycopg.Connection[Any], tenant: str, version: Any, acl: Any, text: str) -> str:
    row = admin.execute(
        """INSERT INTO knowledge_chunks
             (tenant_id, version_id, chunk_kind, chunk_text, chunk_sha256,
              tokenizer_id, lexeme_tokenizer_id, heading_path, locator, acl_policy_id,
              index_generation, lexeme)
           VALUES (%s, %s, 'child', %s, %s, 'approx-cjk-v1', 'cjk-char-v1',
                   ARRAY['第 1 章'], '{}'::jsonb, %s, 1, to_tsvector('simple', %s))
           RETURNING id""",
        (tenant, version, text, hashlib.sha256(text.encode()).hexdigest(), acl, text),
    ).fetchone()
    return str(row[0])


def _ingest(
    conn: psycopg.Connection[Any], tenant: str, actor: str, *, key: str | None = None
) -> Any:
    result = ingest_document(
        conn,
        UUID(tenant),
        IngestionRequest(
            source_id=f"conf-{uuid.uuid4().hex[:8]}",
            filename="catalog.md",
            content=("# 目录\n\nA112420 200mm 39.00\n").encode(),
            knowledge_key=key or f"conf-{uuid.uuid4().hex[:6]}",
            title="目录",
            domain="product_catalog",
            owner_department="sales",
            classification=2,
            audience="internal",
            required_scope="internal_product",
            acl_policy=AclPolicy.from_json({"allow": {"roles": ["staff"]}, "deny": {}}),
            actor_id=UUID(actor),
            catalog_currency="CNY",
        ),
    )
    conn.commit()
    return result


def _setup_conflict(
    admin: psycopg.Connection[Any], app_conn: psycopg.Connection[Any]
) -> dict[str, str]:
    tenant = create_tenant(admin, f"冲突 {uuid.uuid4().hex[:6]}")
    actor = create_principal(admin, tenant, f"ops-{uuid.uuid4().hex[:6]}@example.test")
    admin.commit()

    result = _ingest(app_conn, tenant, actor)  # 先入库一个非冲突货号，拿到版本与 acl
    version = result.version_id
    set_tenant(admin, tenant)
    acl = admin.execute(
        "SELECT acl_policy_id FROM knowledge_chunks WHERE tenant_id = %s AND version_id = %s LIMIT 1",
        (tenant, version),
    ).fetchone()[0]

    _chunk(admin, tenant, version, acl, LINE_A)
    _chunk(admin, tenant, version, acl, LINE_B)
    admin.commit()

    # 精确通道只认可检索版本（DRAFT 查不到），所以先把版本审到 APPROVED_INTERNAL
    review = ReviewService(app_conn, UUID(tenant))
    review.pass_review(version, UUID(actor))
    review.approve_internal(version, UUID(actor))
    app_conn.commit()

    IngestionRepository(app_conn, UUID(tenant)).extract_catalog_items(
        version_id=version, currency="CNY"
    )
    app_conn.commit()
    return {"tenant": tenant, "actor": actor, "version": str(version), "token": _token(tenant, actor)}


def _ask(client: TestClient, token: str, query: str = "A112420 的单价是多少？") -> Any:
    return client.post(
        "/v1/knowledge/answer",
        json={
            "query": query,
            "knowledge_scope": "internal_product",
            "language": "zh-CN",
            "max_evidence": 5,
        },
        headers={"Authorization": f"Bearer {token}"},
    )


def _candidates(admin: psycopg.Connection[Any], tenant: str) -> list[tuple[Any, ...]]:
    set_tenant(admin, tenant)
    return admin.execute(
        """SELECT c.candidate_id, c.chunk_id, c.raw_text, c.spec, c.raw_price
             FROM catalog_conflict_candidates c
             JOIN catalog_conflict_groups g
               ON g.tenant_id = c.tenant_id AND g.group_id = c.group_id
            WHERE c.tenant_id = %s AND g.code = 'A112420'
            ORDER BY c.raw_text""",
        (tenant,),
    ).fetchall()


# ---------------------------------------------------------------------------
# 1. 记录：完整候选与来源
# ---------------------------------------------------------------------------
def test_conflict_records_all_candidates_with_sources(
    admin: psycopg.Connection[Any], app_conn: psycopg.Connection[Any]
) -> None:
    seeded = _setup_conflict(admin, app_conn)
    rows = _candidates(admin, seeded["tenant"])
    raws = [row[2] for row in rows]
    assert len(raws) == 3, f"应包含入库原文与两条冲突行，实际 {raws}"
    assert LINE_A in raws and LINE_B in raws
    assert all(row[1] for row in rows), "每个候选都要保留来源 chunk"
    groups, candidates = open_summary(app_conn, UUID(seeded["tenant"]))
    assert (groups, candidates) == (1, 3), "1 个待办组、3 条候选（含入库原文）"
    # 未决议前：没有条目，问答也拿不到候选价格
    assert admin.execute(
        "SELECT count(*) FROM catalog_items WHERE tenant_id = %s AND code = 'A112420'",
        (seeded["tenant"],),
    ).fetchone()[0] == 0


def test_open_conflict_is_not_answerable(
    admin: psycopg.Connection[Any], app_conn: psycopg.Connection[Any], client: TestClient
) -> None:
    seeded = _setup_conflict(admin, app_conn)
    body = _ask(client, seeded["token"]).json()
    assert body["status"] == "REFUSED"
    assert body["refusal_code"] == "NO_EVIDENCE"
    assert body["citations"] == []


# ---------------------------------------------------------------------------
# 2. 决议：KEEP / DROP_ALL
# ---------------------------------------------------------------------------
def test_resolve_keep_backfills_and_answers(
    admin: psycopg.Connection[Any], app_conn: psycopg.Connection[Any], client: TestClient
) -> None:
    seeded = _setup_conflict(admin, app_conn)
    candidate = next(row for row in _candidates(admin, seeded["tenant"]) if row[2] == LINE_B)

    group = resolve_group(
        app_conn,
        UUID(seeded["tenant"]),
        version_id=UUID(seeded["version"]),
        code="A112420",
        actor_id=UUID(seeded["actor"]),
        candidate_id=UUID(str(candidate[0])),
        note="以 300mm 版本为准",
    )
    app_conn.commit()
    assert group.status == "KEEP"
    assert group.selected_candidate_id == UUID(str(candidate[0]))

    item = admin.execute(
        "SELECT raw_text, raw_price, chunk_id FROM catalog_items WHERE tenant_id = %s AND code = 'A112420'",
        (seeded["tenant"],),
    ).fetchone()
    assert item[0] == LINE_B and item[1] == "48.00" and str(item[2]) == str(candidate[1])

    body = _ask(client, seeded["token"]).json()
    assert body["status"] == "ANSWERED"
    assert "48.00" in body["answer"]
    assert len(body["citations"]) == 1
    assert body["citations"][0]["evidence_id"] == str(candidate[1])


def test_resolve_drop_all_keeps_item_absent(
    admin: psycopg.Connection[Any], app_conn: psycopg.Connection[Any], client: TestClient
) -> None:
    seeded = _setup_conflict(admin, app_conn)
    group = resolve_group(
        app_conn,
        UUID(seeded["tenant"]),
        version_id=UUID(seeded["version"]),
        code="A112420",
        actor_id=UUID(seeded["actor"]),
        drop_all=True,
    )
    app_conn.commit()
    assert group.status == "DROP_ALL"
    assert admin.execute(
        "SELECT count(*) FROM catalog_items WHERE tenant_id = %s AND code = 'A112420'",
        (seeded["tenant"],),
    ).fetchone()[0] == 0
    body = _ask(client, seeded["token"]).json()
    assert body["status"] == "REFUSED"
    assert body["refusal_code"] == "NO_EVIDENCE"


# ---------------------------------------------------------------------------
# 3. 幂等与重复决议
# ---------------------------------------------------------------------------
def test_reextract_after_resolution_respects_decision(
    admin: psycopg.Connection[Any], app_conn: psycopg.Connection[Any]
) -> None:
    seeded = _setup_conflict(admin, app_conn)
    candidate = _candidates(admin, seeded["tenant"])[0]
    resolve_group(
        app_conn,
        UUID(seeded["tenant"]),
        version_id=UUID(seeded["version"]),
        code="A112420",
        actor_id=UUID(seeded["actor"]),
        candidate_id=UUID(str(candidate[0])),
    )
    app_conn.commit()

    repo = IngestionRepository(app_conn, UUID(seeded["tenant"]))
    stats = repo.extract_catalog_items(version_id=UUID(seeded["version"]), currency="CNY")
    app_conn.commit()
    assert stats.conflicts == 1, "已决议组仍算一个冲突组"
    assert admin.execute(
        "SELECT count(*) FROM catalog_items WHERE tenant_id = %s AND code = 'A112420'",
        (seeded["tenant"],),
    ).fetchone()[0] == 1, "KEEP 后重抽取必须恰好一条，且不得覆盖"


def test_second_resolution_is_rejected(
    admin: psycopg.Connection[Any], app_conn: psycopg.Connection[Any]
) -> None:
    seeded = _setup_conflict(admin, app_conn)
    candidate = _candidates(admin, seeded["tenant"])[0]
    resolve_group(
        app_conn,
        UUID(seeded["tenant"]),
        version_id=UUID(seeded["version"]),
        code="A112420",
        actor_id=UUID(seeded["actor"]),
        candidate_id=UUID(str(candidate[0])),
    )
    app_conn.commit()
    with pytest.raises(ConflictError, match="已决议"):
        resolve_group(
            app_conn,
            UUID(seeded["tenant"]),
            version_id=UUID(seeded["version"]),
            code="A112420",
            actor_id=UUID(seeded["actor"]),
            candidate_id=UUID(str(candidate[0])),
        )
    app_conn.rollback()


# ---------------------------------------------------------------------------
# 4. 身份与归属
# ---------------------------------------------------------------------------
def test_forged_or_cross_tenant_actor_is_rejected(
    admin: psycopg.Connection[Any], app_conn: psycopg.Connection[Any]
) -> None:
    seeded = _setup_conflict(admin, app_conn)
    candidate = _candidates(admin, seeded["tenant"])[0]

    with pytest.raises(ConflictError, match="不属于租户"):
        resolve_group(
            app_conn,
            UUID(seeded["tenant"]),
            version_id=UUID(seeded["version"]),
            code="A112420",
            actor_id=uuid.uuid4(),  # 伪造
            candidate_id=UUID(str(candidate[0])),
        )
    app_conn.rollback()

    other = create_tenant(admin, f"外部 {uuid.uuid4().hex[:6]}")
    other_actor = create_principal(admin, other, f"x-{uuid.uuid4().hex[:6]}@example.test")
    admin.commit()
    with pytest.raises(ConflictError, match="不属于租户"):
        resolve_group(
            app_conn,
            UUID(seeded["tenant"]),
            version_id=UUID(seeded["version"]),
            code="A112420",
            actor_id=UUID(other_actor),
            candidate_id=UUID(str(candidate[0])),
        )
    app_conn.rollback()


def test_service_principal_cannot_resolve(
    admin: psycopg.Connection[Any], app_conn: psycopg.Connection[Any]
) -> None:
    seeded = _setup_conflict(admin, app_conn)
    candidate = _candidates(admin, seeded["tenant"])[0]
    agent = uuid.uuid4()
    admin.execute(
        """INSERT INTO principals (tenant_id, id, external_subject, principal_type)
           VALUES (%s, %s, %s, 'agent')""",
        (seeded["tenant"], agent, f"agent-{agent.hex[:6]}"),
    )
    admin.commit()

    with pytest.raises(ConflictError, match="用户主体"):
        resolve_group(
            app_conn,
            UUID(seeded["tenant"]),
            version_id=UUID(seeded["version"]),
            code="A112420",
            actor_id=agent,
            candidate_id=UUID(str(candidate[0])),
        )
    app_conn.rollback()


def test_candidate_from_another_group_is_rejected(
    admin: psycopg.Connection[Any], app_conn: psycopg.Connection[Any]
) -> None:
    seeded = _setup_conflict(admin, app_conn)
    _candidates(admin, seeded["tenant"])  # 确保已有候选，再拿一个不属于该组的 id

    with pytest.raises(ConflictError, match="不属于该待办组"):
        resolve_group(
            app_conn,
            UUID(seeded["tenant"]),
            version_id=UUID(seeded["version"]),
            code="A112420",
            actor_id=UUID(seeded["actor"]),
            candidate_id=uuid.uuid4(),
        )
    app_conn.rollback()


# ---------------------------------------------------------------------------
# 5. 失效版本与并发
# ---------------------------------------------------------------------------
def test_revoked_version_cannot_be_resolved(
    admin: psycopg.Connection[Any], app_conn: psycopg.Connection[Any]
) -> None:
    seeded = _setup_conflict(admin, app_conn)
    candidate = _candidates(admin, seeded["tenant"])[0]
    ReviewService(app_conn, UUID(seeded["tenant"])).revoke(
        UUID(seeded["version"]), UUID(seeded["actor"])
    )
    app_conn.commit()

    with pytest.raises(ConflictError, match="不可再操作"):
        resolve_group(
            app_conn,
            UUID(seeded["tenant"]),
            version_id=UUID(seeded["version"]),
            code="A112420",
            actor_id=UUID(seeded["actor"]),
            candidate_id=UUID(str(candidate[0])),
        )
    app_conn.rollback()
    assert admin.execute(
        "SELECT count(*) FROM catalog_items WHERE tenant_id = %s AND code = 'A112420'",
        (seeded["tenant"],),
    ).fetchone()[0] == 0


def test_concurrent_resolutions_only_one_wins(
    admin: psycopg.Connection[Any], app_conn: psycopg.Connection[Any], pg_url: str
) -> None:
    seeded = _setup_conflict(admin, app_conn)
    candidate = _candidates(admin, seeded["tenant"])[0]
    barrier = threading.Barrier(2)
    results: list[str] = []

    def worker() -> None:
        with psycopg.connect(pg_url) as conn:
            conn.execute(
                "SELECT set_config('app.tenant_id', %s, true)", (seeded["tenant"],)
            )
            barrier.wait(timeout=5)
            try:
                resolve_group(
                    conn,
                    UUID(seeded["tenant"]),
                    version_id=UUID(seeded["version"]),
                    code="A112420",
                    actor_id=UUID(seeded["actor"]),
                    candidate_id=UUID(str(candidate[0])),
                )
                conn.commit()
                results.append("ok")
            except ConflictError:
                conn.rollback()
                results.append("rejected")

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)

    assert sorted(results) == ["ok", "rejected"]


def test_reextract_after_drop_all_keeps_decision(
    admin: psycopg.Connection[Any], app_conn: psycopg.Connection[Any], client: TestClient
) -> None:
    """DROP_ALL 后重抽取：决议保持、条目仍不存在、问答仍拒答且无引用。"""
    seeded = _setup_conflict(admin, app_conn)
    resolve_group(
        app_conn,
        UUID(seeded["tenant"]),
        version_id=UUID(seeded["version"]),
        code="A112420",
        actor_id=UUID(seeded["actor"]),
        drop_all=True,
    )
    app_conn.commit()

    repo = IngestionRepository(app_conn, UUID(seeded["tenant"]))
    stats = repo.extract_catalog_items(version_id=UUID(seeded["version"]), currency="CNY")
    app_conn.commit()
    assert stats.conflicts == 1

    set_tenant(admin, seeded["tenant"])
    group_status = admin.execute(
        "SELECT status FROM catalog_conflict_groups WHERE tenant_id = %s AND code = 'A112420'",
        (seeded["tenant"],),
    ).fetchone()
    assert group_status[0] == "DROP_ALL", "重抽取不得改写已生效的决议"
    assert admin.execute(
        "SELECT count(*) FROM catalog_items WHERE tenant_id = %s AND code = 'A112420'",
        (seeded["tenant"],),
    ).fetchone()[0] == 0

    body = _ask(client, seeded["token"]).json()
    assert body["status"] == "REFUSED"
    assert body["refusal_code"] == "NO_EVIDENCE"
    assert body["citations"] == []


def test_extraction_failure_rolls_back_queue_and_items(
    admin: psycopg.Connection[Any],
    app_conn: psycopg.Connection[Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """抽取中途失败：组、候选、条目都必须回到事务前状态（不得留半套/丢原条目）。"""
    import fkh.workers.repository as repo_module

    seeded = _setup_conflict(admin, app_conn)
    set_tenant(admin, seeded["tenant"])
    # 事务前基线：1 个待办组、3 条候选、0 条 catalog_items
    before_group = admin.execute(
        "SELECT count(*) FROM catalog_conflict_groups WHERE tenant_id = %s", (seeded["tenant"],)
    ).fetchone()[0]
    before_candidates = admin.execute(
        "SELECT count(*) FROM catalog_conflict_candidates WHERE tenant_id = %s",
        (seeded["tenant"],),
    ).fetchone()[0]
    before_items = admin.execute(
        "SELECT count(*) FROM catalog_items WHERE tenant_id = %s", (seeded["tenant"],)
    ).fetchone()[0]

    real_add = repo_module.add_candidates
    calls = {"n": 0}

    def flaky(*args: Any, **kwargs: Any) -> int:
        calls["n"] += 1
        real_add(*args, **kwargs)  # 先真的写入一部分候选
        raise RuntimeError("模拟抽取中途失败")

    monkeypatch.setattr(repo_module, "add_candidates", flaky)

    with (
        pytest.raises(RuntimeError, match="模拟抽取中途失败"),
        app_conn.transaction(),
    ):
        repo_module.IngestionRepository(app_conn, UUID(seeded["tenant"])).extract_catalog_items(
            version_id=UUID(seeded["version"]), currency="CNY"
        )
    app_conn.rollback()

    set_tenant(admin, seeded["tenant"])
    assert calls["n"] >= 1, "必须真的走到了候选写入步骤"
    assert admin.execute(
        "SELECT count(*) FROM catalog_conflict_groups WHERE tenant_id = %s", (seeded["tenant"],)
    ).fetchone()[0] == before_group
    assert admin.execute(
        "SELECT count(*) FROM catalog_conflict_candidates WHERE tenant_id = %s",
        (seeded["tenant"],),
    ).fetchone()[0] == before_candidates
    assert admin.execute(
        "SELECT count(*) FROM catalog_items WHERE tenant_id = %s", (seeded["tenant"],)
    ).fetchone()[0] == before_items


def test_failure_after_removing_existing_item_restores_it(
    admin: psycopg.Connection[Any],
    app_conn: psycopg.Connection[Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """非空条目基线：冲突处理会先删旧条目，中途失败必须把它完整恢复。"""
    import fkh.workers.repository as repo_module

    tenant = create_tenant(admin, f"回滚 {uuid.uuid4().hex[:6]}")
    actor = create_principal(admin, tenant, f"ops-{uuid.uuid4().hex[:6]}@example.test")
    admin.commit()

    result = _ingest(app_conn, tenant, actor)  # 单行目录：正常生成 1 条条目
    version = result.version_id
    review = ReviewService(app_conn, UUID(tenant))
    review.pass_review(version, UUID(actor))
    review.approve_internal(version, UUID(actor))
    app_conn.commit()

    set_tenant(admin, tenant)
    before = admin.execute(
        """SELECT chunk_id, raw_text, spec, raw_price, price, currency
             FROM catalog_items WHERE tenant_id = %s AND code = 'A112420'""",
        (tenant,),
    ).fetchone()
    assert before is not None, "基线必须是非空条目"
    acl = admin.execute(
        "SELECT acl_policy_id FROM knowledge_chunks WHERE tenant_id = %s AND version_id = %s LIMIT 1",
        (tenant, version),
    ).fetchone()[0]
    _chunk(admin, tenant, version, acl, LINE_B)  # 制造冲突：同货号不同原文行
    admin.commit()

    real_add = repo_module.add_candidates
    calls = {"n": 0}

    def flaky(*args: Any, **kwargs: Any) -> int:
        calls["n"] += 1
        real_add(*args, **kwargs)
        raise RuntimeError("模拟候选写入后失败")

    monkeypatch.setattr(repo_module, "add_candidates", flaky)
    with (
        pytest.raises(RuntimeError, match="模拟候选写入后失败"),
        app_conn.transaction(),
    ):
        repo_module.IngestionRepository(app_conn, UUID(tenant)).extract_catalog_items(
            version_id=version, currency="CNY"
        )
    app_conn.rollback()

    set_tenant(admin, tenant)
    assert calls["n"] >= 1
    after = admin.execute(
        """SELECT chunk_id, raw_text, spec, raw_price, price, currency
             FROM catalog_items WHERE tenant_id = %s AND code = 'A112420'""",
        (tenant,),
    ).fetchone()
    assert after is not None, "回滚后原条目必须还在（删旧条目那一步不能吞掉它）"
    assert str(after[0]) == str(before[0])
    assert str(after[1]) == str(before[1])
    assert str(after[2]) == str(before[2])
    assert str(after[3]) == str(before[3])
    assert str(after[4]) == str(before[4])
    assert str(after[5]) == str(before[5])

    assert admin.execute(
        "SELECT count(*) FROM catalog_conflict_groups WHERE tenant_id = %s", (tenant,)
    ).fetchone()[0] == 0
    assert admin.execute(
        "SELECT count(*) FROM catalog_conflict_candidates WHERE tenant_id = %s", (tenant,)
    ).fetchone()[0] == 0
