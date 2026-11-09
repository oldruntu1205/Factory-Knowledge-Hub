"""检索统计表：与 live 统计等价、失效回退、generation 替换与 RLS。"""

from __future__ import annotations

import threading
import time
import uuid
from typing import Any

import psycopg
import pytest
from tests.conftest import create_principal, create_tenant

from fkh.authz.policy import Subject
from fkh.domain.lexeme import segment_cjk
from fkh.retrieval import stats as stats_module
from fkh.retrieval.lexical import LexicalRetriever
from fkh.retrieval.stats import load_snapshot, refresh_tenant

pytestmark = pytest.mark.requires_pg

SCOPE = "internal_product"
TERMS = ["冷", "凝", "管", "规", "格"]


def _subject(tenant: str, principal: str, *, departments: tuple[str, ...] = ()) -> Subject:
    return Subject(
        principal_id=principal,
        tenant_id=tenant,
        roles=frozenset({"staff"}),
        departments=frozenset(departments),
    )


def _seed_tenant(
    conn: psycopg.Connection[Any],
    *,
    name: str,
    chunks: list[str],
    deny_department: str | None = None,
) -> dict[str, str]:
    tenant = create_tenant(conn, name)
    principal = create_principal(conn, tenant, f"{name}@example.test")
    acl = str(uuid.uuid4())
    policy: dict[str, Any] = {"allow": {"roles": ["staff"]}}
    if deny_department:
        policy["deny"] = {"departments": [deny_department]}
    conn.execute(
        "INSERT INTO acl_policies (id, tenant_id, policy_json) VALUES (%s, %s, %s)",
        (acl, tenant, psycopg.types.json.Jsonb(policy)),
    )
    doc = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO knowledge_documents"
        " (id, tenant_id, knowledge_key, domain, owner_department)"
        " VALUES (%s, %s, %s, 'product_catalog', 'engineering')",
        (doc, tenant, f"stats-{uuid.uuid4().hex[:8]}"),
    )
    version = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO knowledge_versions
             (id, tenant_id, document_id, version_no, title, classification, audience,
              status, required_scope, content_sha256, source_object_key)
           VALUES (%s, %s, %s, 1, %s, 1, 'internal', 'APPROVED_INTERNAL', %s, %s, 'x')""",
        (version, tenant, doc, name, SCOPE, "0" * 64),
    )
    for text in chunks:
        conn.execute(
            """INSERT INTO knowledge_chunks
                 (tenant_id, id, version_id, chunk_kind, chunk_text, chunk_sha256,
                  tokenizer_id, lexeme_tokenizer_id, heading_path, locator, metadata,
                  acl_policy_id, index_generation, lexeme)
               VALUES (%s, %s, %s, 'child', %s, %s, 'approx-cjk-v1', 'cjk-char-v1',
                       '{}', '{}', '{}', %s, 1, to_tsvector('simple', %s))""",
            (tenant, str(uuid.uuid4()), version, text, "0" * 64, acl, segment_cjk(text)),
        )
    conn.commit()
    return {"tenant": tenant, "principal": principal, "acl": acl, "version": version, "doc": doc}


def _refresh(conn: psycopg.Connection[Any], tenant: str) -> dict[str, Any]:
    conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
    summary = refresh_tenant(conn, uuid.UUID(tenant))
    conn.commit()
    return summary


def _stats(conn: psycopg.Connection[Any], seed: dict[str, str], subject: Subject) -> tuple[int, float, dict[str, int]]:
    retriever = LexicalRetriever(conn, uuid.UUID(seed["tenant"]))
    return retriever._corpus_stats(  # noqa: SLF001
        knowledge_scope=SCOPE, subject=subject, terms=TERMS
    )


def test_stats_match_live_and_use_table(pg_url: str, admin: psycopg.Connection[Any]) -> None:
    seed = _seed_tenant(admin, name=f"s-{uuid.uuid4().hex[:6]}", chunks=[
        "冷凝管 24/29 规格", "试管 规格 16mm", "烧杯 规格 250ml",
    ])
    subject = _subject(seed["tenant"], seed["principal"])

    # 未刷新：state 尚未构建 → live
    before = _stats(admin, seed, subject)
    admin.commit()
    _refresh(admin, seed["tenant"])
    after = _stats(admin, seed, subject)

    assert after == before
    assert after[0] == 3
    assert after[2]["冷"] == 1


def test_stats_and_live_top10_are_equivalent(
    pg_url: str, admin: psycopg.Connection[Any]
) -> None:
    seed = _seed_tenant(
        admin, name=f"s-top10-{uuid.uuid4().hex[:6]}",
        chunks=["冷凝管 24/29 规格", "试管 16mm 规格", "烧杯 250ml 规格", "容量瓶 100ml 规格"],
    )
    subject = _subject(seed["tenant"], seed["principal"])
    retriever = LexicalRetriever(admin, uuid.UUID(seed["tenant"]))
    queries = ("冷凝管规格", "试管有哪些", "容量瓶刻度")

    before = [
        [(hit.chunk_id, hit.score) for hit in retriever.search(
            query, knowledge_scope=SCOPE, subject=subject, max_results=10,
        ).hits]
        for query in queries
    ]
    admin.commit()
    _refresh(admin, seed["tenant"])
    after = [
        [(hit.chunk_id, hit.score) for hit in retriever.search(
            query, knowledge_scope=SCOPE, subject=subject, max_results=10,
        ).hits]
        for query in queries
    ]
    assert after == before


def test_stats_permission_denied_falls_back_without_aborting_tx(
    pg_url: str, admin: psycopg.Connection[Any], app_role: str
) -> None:
    seed = _seed_tenant(admin, name=f"s-priv-{uuid.uuid4().hex[:6]}", chunks=["冷凝管 规格"])
    _refresh(admin, seed["tenant"])
    admin.execute(
        "REVOKE SELECT ON retrieval_stats_state, retrieval_corpus_stats, retrieval_term_stats"
        f" FROM {app_role}"
    )
    admin.commit()
    try:
        with psycopg.connect(pg_url) as conn:
            conn.execute(f"SET ROLE {app_role}")
            conn.execute("SELECT set_config('app.tenant_id', %s, false)", (seed["tenant"],))
            retriever = LexicalRetriever(conn, uuid.UUID(seed["tenant"]))
            n, _avg, _df = retriever._corpus_stats(  # noqa: SLF001
                knowledge_scope=SCOPE,
                subject=_subject(seed["tenant"], seed["principal"]),
                terms=TERMS,
            )
            assert n == 1
    finally:
        admin.execute(
            "GRANT SELECT ON retrieval_stats_state, retrieval_corpus_stats,"
            f" retrieval_term_stats TO {app_role}"
        )
        admin.commit()


def test_refresh_requires_repeatable_read(
    pg_url: str, admin: psycopg.Connection[Any]
) -> None:
    tenant = create_tenant(admin, f"s-iso-{uuid.uuid4().hex[:6]}")
    admin.commit()
    with pytest.raises(RuntimeError, match="REPEATABLE READ"):
        refresh_tenant(admin, uuid.UUID(tenant))


def test_acl_groups_are_evaluated_per_subject(
    pg_url: str, admin: psycopg.Connection[Any]
) -> None:
    seed = _seed_tenant(
        admin, name=f"s-acl-{uuid.uuid4().hex[:6]}",
        chunks=["冷凝管 24/29 规格"], deny_department="blocked",
    )
    allowed = _subject(seed["tenant"], seed["principal"])
    denied = _subject(seed["tenant"], seed["principal"], departments=("blocked",))
    _refresh(admin, seed["tenant"])

    assert _stats(admin, seed, allowed)[0] == 1
    assert _stats(admin, seed, denied)[0] == 0


def test_stale_revision_falls_back_live(
    pg_url: str, admin: psycopg.Connection[Any]
) -> None:
    seed = _seed_tenant(admin, name=f"s-stale-{uuid.uuid4().hex[:6]}", chunks=["冷凝管 规格"])
    _refresh(admin, seed["tenant"])
    state = admin.execute(
        "SELECT source_revision, built_revision FROM retrieval_stats_state WHERE tenant_id=%s",
        (seed["tenant"],),
    ).fetchone()
    assert state is not None and state[0] == state[1]

    # 同一事务插入 3 条 chunk：source_revision 只应 +1
    with admin.transaction():
        for text in ("试管 规格", "烧杯 规格", "容量瓶 规格"):
            admin.execute(
                """INSERT INTO knowledge_chunks
                     (tenant_id, id, version_id, chunk_kind, chunk_text, chunk_sha256,
                      tokenizer_id, lexeme_tokenizer_id, heading_path, locator, metadata,
                      acl_policy_id, index_generation, lexeme)
                   VALUES (%s, %s, %s, 'child', %s, %s, 'approx-cjk-v1', 'cjk-char-v1',
                           '{}', '{}', '{}', %s, 1, to_tsvector('simple', %s))""",
                (
                    seed["tenant"], str(uuid.uuid4()), seed["version"], text, "0" * 64,
                    seed["acl"], segment_cjk(text),
                ),
            )
    state = admin.execute(
        "SELECT source_revision, built_revision FROM retrieval_stats_state WHERE tenant_id=%s",
        (seed["tenant"],),
    ).fetchone()
    assert state is not None and state[0] == state[1] + 1
    subject = _subject(seed["tenant"], seed["principal"])
    assert _stats(admin, seed, subject)[0] == 4  # live fallback sees all four


def test_refresh_retries_serialization_failure(
    pg_url: str, admin: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant = create_tenant(admin, f"s-retry-{uuid.uuid4().hex[:6]}")
    admin.commit()
    original = stats_module.refresh_tenant
    calls = {"n": 0}

    def flaky(conn: psycopg.Connection[Any], target: uuid.UUID) -> dict[str, Any]:
        calls["n"] += 1
        if calls["n"] < 3:
            raise psycopg.errors.SerializationFailure("synthetic 40001")
        return original(conn, target)

    monkeypatch.setattr(stats_module, "refresh_tenant", flaky)
    summary = stats_module._refresh_one(pg_url, uuid.UUID(tenant))  # noqa: SLF001
    assert calls["n"] == 3
    assert summary["status"] == "EMPTY"


def test_refresh_recovers_from_real_snapshot_conflict(
    pg_url: str, admin: psycopg.Connection[Any]
) -> None:
    seed = _seed_tenant(admin, name=f"s-real-40001-{uuid.uuid4().hex[:6]}", chunks=["冷凝管 规格"])
    _refresh(admin, seed["tenant"])

    blocker = psycopg.connect(pg_url, autocommit=False)
    blocker.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
    blocker.execute(
        "SELECT tenant_id FROM retrieval_stats_state WHERE tenant_id=%s FOR UPDATE",
        (seed["tenant"],),
    )
    blocker.execute(
        "UPDATE retrieval_stats_state SET source_revision = source_revision + 1"
        " WHERE tenant_id=%s",
        (seed["tenant"],),
    )

    result: dict[str, Any] = {}
    error: dict[str, BaseException] = {}

    def refresh_in_thread() -> None:
        try:
            result.update(stats_module._refresh_one(pg_url, uuid.UUID(seed["tenant"])))  # noqa: SLF001
        except BaseException as exc:  # noqa: BLE001 - 线程内异常带回主线程断言
            error["exc"] = exc

    thread = threading.Thread(target=refresh_in_thread, daemon=True)
    thread.start()
    try:
        time.sleep(0.3)
        assert thread.is_alive(), "刷新应在状态行锁上等待"
    finally:
        blocker.commit()
        blocker.close()
    thread.join(timeout=20)
    assert not thread.is_alive()
    assert not error, error
    assert result["status"] in {"READY", "EMPTY"}
    state = admin.execute(
        "SELECT status, built_revision, source_revision FROM retrieval_stats_state"
        " WHERE tenant_id=%s",
        (seed["tenant"],),
    ).fetchone()
    assert state is not None and state[1] == state[2]


def test_repeatable_read_conflict_is_serialization_failure(
    pg_url: str, admin: psycopg.Connection[Any]
) -> None:
    seed = _seed_tenant(admin, name=f"s-conflict-{uuid.uuid4().hex[:6]}", chunks=["冷凝管 规格"])
    _refresh(admin, seed["tenant"])

    first = psycopg.connect(pg_url, autocommit=False)
    second = psycopg.connect(pg_url, autocommit=False)
    try:
        for conn in (first, second):
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            conn.execute(
                "SELECT source_revision FROM retrieval_stats_state WHERE tenant_id=%s",
                (seed["tenant"],),
            )
        first.execute(
            "UPDATE retrieval_stats_state SET source_revision = source_revision + 1"
            " WHERE tenant_id=%s",
            (seed["tenant"],),
        )
        first.commit()
        with pytest.raises(psycopg.errors.SerializationFailure):
            second.execute(
                "UPDATE retrieval_stats_state SET source_revision = source_revision + 1"
                " WHERE tenant_id=%s",
                (seed["tenant"],),
            )
    finally:
        first.close()
        second.rollback()
        second.close()


def test_refresh_gives_up_after_three_serialization_failures(
    pg_url: str, admin: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant = create_tenant(admin, f"s-retry-fail-{uuid.uuid4().hex[:6]}")
    admin.commit()
    calls = {"n": 0}

    def always(conn: psycopg.Connection[Any], target: uuid.UUID) -> dict[str, Any]:
        calls["n"] += 1
        raise psycopg.errors.SerializationFailure("synthetic 40001")

    monkeypatch.setattr(stats_module, "refresh_tenant", always)
    with pytest.raises(psycopg.errors.SerializationFailure):
        stats_module._refresh_one(pg_url, uuid.UUID(tenant))  # noqa: SLF001
    assert calls["n"] == 3


def test_write_waits_for_refresh_lock_and_invalidates(
    pg_url: str, admin: psycopg.Connection[Any]
) -> None:
    seed = _seed_tenant(admin, name=f"s-lock-{uuid.uuid4().hex[:6]}", chunks=["冷凝管 规格"])
    _refresh(admin, seed["tenant"])

    # A 模拟重建持有状态行锁；B 的写触发器必须等待，不能把 built=source 错误保持为真。
    locker = psycopg.connect(pg_url, autocommit=False)
    locker.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
    locker.execute(
        "SELECT tenant_id FROM retrieval_stats_state WHERE tenant_id=%s FOR UPDATE",
        (seed["tenant"],),
    )

    done = threading.Event()

    def writer() -> None:
        with psycopg.connect(pg_url, autocommit=False) as conn:
            conn.execute(
                """INSERT INTO knowledge_chunks
                     (tenant_id, id, version_id, chunk_kind, chunk_text, chunk_sha256,
                      tokenizer_id, lexeme_tokenizer_id, heading_path, locator, metadata,
                      acl_policy_id, index_generation, lexeme)
                   VALUES (%s, %s, %s, 'child', %s, %s, 'approx-cjk-v1', 'cjk-char-v1',
                           '{}', '{}', '{}', %s, 1, to_tsvector('simple', %s))""",
                (
                    seed["tenant"], str(uuid.uuid4()), seed["version"], "试管 规格",
                    "0" * 64, seed["acl"], segment_cjk("试管 规格"),
                ),
            )
            conn.commit()
        done.set()

    thread = threading.Thread(target=writer, daemon=True)
    thread.start()
    try:
        time.sleep(0.3)
        assert not done.is_set(), "写触发器应被重建状态行锁阻塞"
    finally:
        locker.commit()
        locker.close()
    thread.join(timeout=10)
    assert done.is_set()

    state = admin.execute(
        "SELECT source_revision, built_revision FROM retrieval_stats_state WHERE tenant_id=%s",
        (seed["tenant"],),
    ).fetchone()
    assert state is not None and state[0] > state[1]


def test_dirty_refresh_and_watch(pg_url: str, admin: psycopg.Connection[Any]) -> None:
    seed = _seed_tenant(admin, name=f"s-dirty-{uuid.uuid4().hex[:6]}", chunks=["冷凝管 规格"])
    _refresh(admin, seed["tenant"])
    admin.execute(
        """INSERT INTO knowledge_chunks
             (tenant_id, id, version_id, chunk_kind, chunk_text, chunk_sha256,
              tokenizer_id, lexeme_tokenizer_id, heading_path, locator, metadata,
              acl_policy_id, index_generation, lexeme)
           VALUES (%s, %s, %s, 'child', %s, %s, 'approx-cjk-v1', 'cjk-char-v1',
                   '{}', '{}', '{}', %s, 1, to_tsvector('simple', %s))""",
        (
            seed["tenant"], str(uuid.uuid4()), seed["version"], "试管 规格",
            "0" * 64, seed["acl"], segment_cjk("试管 规格"),
        ),
    )
    admin.commit()

    dirty = stats_module.dirty_tenants(admin)
    assert uuid.UUID(seed["tenant"]) in dirty
    result = stats_module.refresh_dirty_once(pg_url)
    assert result["refreshed"] >= 1
    assert not result["failed"]
    state = admin.execute(
        "SELECT status, built_revision, source_revision FROM retrieval_stats_state"
        " WHERE tenant_id=%s",
        (seed["tenant"],),
    ).fetchone()
    assert state is not None and state[0] == "READY" and state[1] == state[2]

    # watch 单轮：再制造一次 dirty，watch 应刷新后退出 0
    admin.execute(
        "UPDATE knowledge_versions SET status = 'APPROVED_INTERNAL' WHERE id = %s",
        (seed["version"],),
    )
    admin.commit()
    assert stats_module._watch(pg_url, interval=0.2, max_iterations=1) == 0


def test_refresh_failure_marks_failed_and_falls_back(
    pg_url: str, admin: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    seed = _seed_tenant(admin, name=f"s-fail-{uuid.uuid4().hex[:6]}", chunks=["冷凝管 规格"])
    _refresh(admin, seed["tenant"])
    admin.execute(
        """INSERT INTO knowledge_chunks
             (tenant_id, id, version_id, chunk_kind, chunk_text, chunk_sha256,
              tokenizer_id, lexeme_tokenizer_id, heading_path, locator, metadata,
              acl_policy_id, index_generation, lexeme)
           VALUES (%s, %s, %s, 'child', %s, %s, 'approx-cjk-v1', 'cjk-char-v1',
                   '{}', '{}', '{}', %s, 1, to_tsvector('simple', %s))""",
        (
            seed["tenant"], str(uuid.uuid4()), seed["version"], "试管 规格",
            "0" * 64, seed["acl"], segment_cjk("试管 规格"),
        ),
    )
    admin.commit()

    def boom(conn: psycopg.Connection[Any], tenant: uuid.UUID) -> dict[str, Any]:
        raise RuntimeError("synthetic refresh failure")

    # 打真实 refresh_tenant，而不是 _refresh_one：让 _refresh_one 的真实连接/事务路径
    # 与 refresh_dirty_once 的真实 mark_failed 链路都被覆盖。
    monkeypatch.setattr(stats_module, "refresh_tenant", boom)
    result = stats_module.refresh_dirty_once(pg_url)
    assert result["failed"] and any(
        item["tenant_id"] == seed["tenant"] for item in result["failed"]
    )
    state = admin.execute(
        "SELECT status, last_error, source_revision, built_revision"
        " FROM retrieval_stats_state WHERE tenant_id=%s",
        (seed["tenant"],),
    ).fetchone()
    assert state is not None
    assert state[0] == "FAILED"
    assert "synthetic refresh failure" in str(state[1])
    assert state[2] > state[3]
    # FAILED 仍不能阻断检索：回退 live 能看到新增块
    retriever = LexicalRetriever(admin, uuid.UUID(seed["tenant"]))
    n, _avg, _df = retriever._corpus_stats(  # noqa: SLF001
        knowledge_scope=SCOPE,
        subject=_subject(seed["tenant"], seed["principal"]),
        terms=TERMS,
    )
    assert n == 2


def test_one_tenant_failure_does_not_block_others(
    pg_url: str, admin: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    bad = _seed_tenant(admin, name=f"s-bad-{uuid.uuid4().hex[:6]}", chunks=["冷凝管 规格"])
    good = _seed_tenant(admin, name=f"s-good-{uuid.uuid4().hex[:6]}", chunks=["试管 规格"])
    original = stats_module.refresh_tenant

    def selective(conn: psycopg.Connection[Any], tenant: uuid.UUID) -> dict[str, Any]:
        if tenant == uuid.UUID(bad["tenant"]):
            raise RuntimeError("synthetic tenant failure")
        return original(conn, tenant)

    monkeypatch.setattr(stats_module, "refresh_tenant", selective)
    result = stats_module.refresh_dirty_once(
        pg_url, tenants=[uuid.UUID(bad["tenant"]), uuid.UUID(good["tenant"])]
    )
    assert [item["tenant_id"] for item in result["failed"]] == [bad["tenant"]]
    assert result["refreshed"] == 1
    good_state = admin.execute(
        "SELECT status, built_revision, source_revision FROM retrieval_stats_state"
        " WHERE tenant_id=%s",
        (good["tenant"],),
    ).fetchone()
    assert good_state is not None and good_state[0] == "READY" and good_state[1] == good_state[2]
    # 更旧的失败不能把已经 READY 的新状态改成 FAILED
    stats_module.mark_failed(pg_url, uuid.UUID(good["tenant"]), "late stale failure")
    still_good = admin.execute(
        "SELECT status FROM retrieval_stats_state WHERE tenant_id=%s",
        (good["tenant"],),
    ).fetchone()
    assert still_good is not None and still_good[0] == "READY"


def test_alert_webhook_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[Any] = []

    class Response:
        def read(self, size: int = -1) -> bytes:
            return b"ok"

        def __enter__(self) -> Response:
            return self

        def __exit__(self, *args: object) -> None:
            return None

    def fake_urlopen(request: Any, timeout: int) -> Response:
        sent.append(request)
        return Response()

    monkeypatch.setattr(stats_module.urllib.request, "urlopen", fake_urlopen)
    stats_module._send_alert(  # noqa: SLF001
        "https://alerts.example.test/fkh",
        {"event": "retrieval_stats_alert", "failed": [{"tenant_id": "t"}]},
    )
    assert len(sent) == 1
    assert sent[0].full_url == "https://alerts.example.test/fkh"
    assert b"retrieval_stats_alert" in sent[0].data


def test_rebuild_replaces_generation(pg_url: str, admin: psycopg.Connection[Any]) -> None:
    seed = _seed_tenant(admin, name=f"s-gen-{uuid.uuid4().hex[:6]}", chunks=["冷凝管 规格"])
    first = _refresh(admin, seed["tenant"])
    second = _refresh(admin, seed["tenant"])
    assert second["generation"] == first["generation"] + 1
    old = admin.execute(
        "SELECT count(*) FROM retrieval_corpus_stats WHERE tenant_id=%s AND generation=%s",
        (seed["tenant"], first["generation"]),
    ).fetchone()
    assert old is not None and old[0] == 0
    state = admin.execute(
        "SELECT built_revision, source_revision FROM retrieval_stats_state WHERE tenant_id=%s",
        (seed["tenant"],),
    ).fetchone()
    assert state is not None and state[0] == state[1]


def test_empty_corpus_is_distinct_from_unbuilt(
    pg_url: str, admin: psycopg.Connection[Any]
) -> None:
    tenant = create_tenant(admin, f"s-empty-{uuid.uuid4().hex[:6]}")
    admin.commit()
    summary = _refresh(admin, tenant)
    assert summary["status"] == "EMPTY"
    snapshot = load_snapshot(admin, uuid.UUID(tenant), scope=SCOPE, internal=True, terms=TERMS)
    assert snapshot is not None and snapshot.n == 0


def test_stats_table_respects_tenant_rls(
    pg_url: str, admin: psycopg.Connection[Any], app_role: str
) -> None:
    seed_a = _seed_tenant(admin, name=f"rls-a-{uuid.uuid4().hex[:6]}", chunks=["冷凝管 规格"])
    seed_b = _seed_tenant(admin, name=f"rls-b-{uuid.uuid4().hex[:6]}", chunks=["试管 规格"])
    _refresh(admin, seed_a["tenant"])
    _refresh(admin, seed_b["tenant"])

    with psycopg.connect(pg_url) as conn:
        conn.execute(f"SET ROLE {app_role}")
        conn.execute("SELECT set_config('app.tenant_id', '', false)")
        assert conn.execute("SELECT count(*) FROM retrieval_corpus_stats").fetchone()[0] == 0
        conn.execute("SELECT set_config('app.tenant_id', %s, false)", (seed_a["tenant"],))
        rows = conn.execute(
            "SELECT tenant_id FROM retrieval_corpus_stats ORDER BY tenant_id"
        ).fetchall()
        assert [str(row[0]) for row in rows] == [seed_a["tenant"]]
