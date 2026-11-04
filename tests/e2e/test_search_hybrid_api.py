"""混合检索接线验证（`POST /v1/knowledge/search`）。

逐条对应 2026-11-04 L1 判决 Q1–Q7 的「必须补的断言」：
契约取值、默认模式、缺模型 fail-closed、审计模式一致、融合后 ACL、深度上限、
以及 context 端点保持词法。

这一层验证的是**接线**，不是模型质量：真实 BGE-M3 用确定性伪向量替代，
但「双路都执行、模式如实上报、失败不伪装成功」这些工程约束必须是真的。
"""

from __future__ import annotations

import hashlib
import inspect
import json
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import jwt
import psycopg
import pytest
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator, FormatChecker
from tests.conftest import (
    create_acl,
    create_document,
    create_principal,
    create_tenant,
    create_version,
)

from fkh.api.main import create_app
from fkh.config import Settings
from fkh.domain.lexeme import segment_cjk
from fkh.retrieval import hybrid as hybrid_module
from fkh.retrieval.embedding import (
    DeterministicTestEmbedder,
    EmbeddingError,
    EmbeddingNotConfigured,
    build_embedder,
    get_embedder,
    to_pgvector,
)
from fkh.retrieval.hybrid import HybridRetriever, reciprocal_rank_fusion
from fkh.retrieval.lexical import MAX_CANDIDATES, OVERFETCH
from fkh.retrieval.search import ERROR_EMBEDDING_UNAVAILABLE

pytestmark = pytest.mark.requires_pg

SECRET = "hybrid-api-secret-0123456789abcdef"
HASH_KEY = "hybrid-audit-hmac-key-0123456789"
HASH_KEY_ID = "k-hybrid"
ROOT = Path(__file__).resolve().parents[2]
SEARCH_SCHEMA = json.loads(
    (ROOT / "contracts" / "schema" / "search_response.json").read_text(encoding="utf-8")
)
TEXT = "冷凝管规格说明：高硼硅玻璃，接口 24/29 与 29/32。"
DENSE_ONLY_TEXT = "办公用品领用登记表：签字后到行政领取。"


# ---------------------------------------------------------------------------
# 夹具与工具
# ---------------------------------------------------------------------------
def _token(
    tenant_id: str,
    principal_id: str,
    *,
    scopes: tuple[str, ...] = ("knowledge.search",),
    allowed: tuple[str, ...] = ("internal_product",),
    roles: tuple[str, ...] = ("staff",),
) -> str:
    now = datetime.now(UTC)
    payload = {
        "iss": "fkh.local",
        "aud": "fkh-api",
        "sub": f"user-{principal_id[:8]}@example.test",
        "iat": now,
        "exp": now + timedelta(minutes=10),
        "tenant_id": tenant_id,
        "principal_id": principal_id,
        "roles": list(roles),
        "scope": list(scopes),
        "allowed_scopes": list(allowed),
    }
    return jwt.encode(payload, SECRET, algorithm="HS256")


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def make_client(pg_url: str) -> Iterator[Any]:
    """按需构造不同配置的应用实例（默认配置 / 混合 / 参数错误）。"""
    clients: list[TestClient] = []

    def _make(**overrides: Any) -> TestClient:
        settings = Settings(
            jwt_secret=SECRET,
            database_url=pg_url,
            retrieval_hash_key=HASH_KEY,
            retrieval_hash_key_id=HASH_KEY_ID,
            **overrides,
        )
        client = TestClient(create_app(settings), raise_server_exceptions=False)
        client.__enter__()
        clients.append(client)
        return client

    yield _make
    for client in clients:
        client.__exit__(None, None, None)


@pytest.fixture
def tenant(admin: psycopg.Connection[Any]) -> str:
    tenant_id = create_tenant(admin, f"混合租户 {uuid.uuid4().hex[:6]}")
    admin.commit()
    return tenant_id


def _version(
    admin: psycopg.Connection[Any],
    tenant_id: str,
    *,
    status: str = "APPROVED_INTERNAL",
    required_scope: str | None = "internal_product",
    audience: str = "internal",
) -> str:
    approver = create_principal(admin, tenant_id, f"approver-{uuid.uuid4().hex[:6]}@example.test")
    second = create_principal(admin, tenant_id, f"approver2-{uuid.uuid4().hex[:6]}@example.test")
    doc = create_document(admin, tenant_id, f"doc-{uuid.uuid4().hex[:8]}")
    version = create_version(
        admin,
        tenant_id,
        doc,
        status=status,
        classification=0,
        audience=audience,
        approved_by=approver if status != "DRAFT" else None,
        public_approved_by=second if status == "PUBLISHED_PUBLIC" else None,
    )
    if required_scope is not None:
        admin.execute(
            "UPDATE knowledge_versions SET required_scope = %s WHERE id = %s",
            (required_scope, version),
        )
    return version


def _chunk(
    admin: psycopg.Connection[Any],
    tenant_id: str,
    version_id: str,
    acl_id: str,
    *,
    text: str = TEXT,
    embedding: list[float] | None = None,
    embedding_model_id: str | None = None,
) -> str:
    row = admin.execute(
        """INSERT INTO knowledge_chunks
             (tenant_id, version_id, parent_chunk_id, chunk_kind, chunk_text, chunk_sha256,
              tokenizer_id, lexeme_tokenizer_id, heading_path, locator, acl_policy_id,
              index_generation, lexeme, embedding, embedding_model_id)
           VALUES (%s, %s, NULL, 'child', %s, %s, 'approx-cjk-v1', 'cjk-char-v1',
                   ARRAY['第 1 章'], '{"block_index": 0}'::jsonb, %s, 1,
                   to_tsvector('simple', %s), %s::vector, %s)
           RETURNING id""",
        (
            tenant_id,
            version_id,
            text,
            hashlib.sha256(text.encode()).hexdigest(),
            acl_id,
            segment_cjk(text),
            to_pgvector(embedding) if embedding is not None else None,
            embedding_model_id,
        ),
    ).fetchone()
    assert row is not None
    return str(row[0])


def _search(client: TestClient, token: str, query: str = "冷凝管规格", **overrides: Any) -> Any:
    payload = {"query": query, "knowledge_scope": "internal_product", "max_results": 10}
    payload.update(overrides)
    return client.post("/v1/knowledge/search", json=payload, headers=_auth(token))


def _ids(response: Any) -> set[str]:
    return {item["chunk_id"] for item in response.json()["results"]}


def _audit_rows(admin: psycopg.Connection[Any], tenant_id: str) -> list[tuple[Any, ...]]:
    rows = admin.execute(
        """SELECT status, refusal_code, returned_count, trace_id, retrieval_mode
             FROM retrieval_queries WHERE tenant_id = %s ORDER BY created_at, id""",
        (tenant_id,),
    ).fetchall()
    admin.rollback()
    return rows


def _assert_schema(body: dict[str, Any]) -> None:
    Draft202012Validator(SEARCH_SCHEMA, format_checker=FormatChecker()).validate(body)


def _staff(admin: psycopg.Connection[Any], tenant_id: str) -> tuple[str, str]:
    principal = create_principal(admin, tenant_id, f"staff-{uuid.uuid4().hex[:6]}@example.test")
    return principal, _token(tenant_id, principal)


# ---------------------------------------------------------------------------
# Q1 / Q2：默认词法、混合才上报 hybrid、schema 双向一致
# ---------------------------------------------------------------------------
def test_default_config_is_lexical_and_schema_valid(
    make_client: Any, admin: psycopg.Connection[Any], tenant: str
) -> None:
    version = _version(admin, tenant)
    chunk = _chunk(admin, tenant, version, create_acl(admin, tenant, {"allow": {"roles": ["staff"]}}))
    _, token = _staff(admin, tenant)
    admin.commit()

    client = make_client()  # 不传 retrieval_mode → 默认 lexical
    response = _search(client, token)
    assert response.status_code == 200
    body = response.json()
    assert body["scoring"] == "lexical-v1"
    _assert_schema(body)
    assert chunk in _ids(response)
    assert [(row[3], row[4]) for row in _audit_rows(admin, tenant)] == [
        (body["trace_id"], "lexical-v1")
    ]


def test_hybrid_executes_both_paths_and_reports_hybrid_scoring(
    make_client: Any, admin: psycopg.Connection[Any], tenant: str
) -> None:
    embedder = DeterministicTestEmbedder()
    version = _version(admin, tenant)
    acl = create_acl(admin, tenant, {"allow": {"roles": ["staff"]}})
    lexical_hit = _chunk(admin, tenant, version, acl)
    # 词法查不到、只有向量能命中的块：向量 = 查询向量本身
    dense_only = _chunk(
        admin,
        tenant,
        version,
        acl,
        text=DENSE_ONLY_TEXT,
        embedding=embedder.embed_query("冷凝管规格"),
        embedding_model_id=embedder.model_id,
    )
    _, token = _staff(admin, tenant)
    admin.commit()

    hybrid = _search(make_client(retrieval_mode="hybrid", embedding_provider="test"), token)
    assert hybrid.status_code == 200
    body = hybrid.json()
    assert body["scoring"] == "hybrid-rrf-v1"
    _assert_schema(body)
    found = _ids(hybrid)
    assert lexical_hit in found, "词法路命中不能因融合丢失"
    assert dense_only in found, "向量路命中的块说明双路都执行了"

    # 同一份数据走纯词法：向量独有的块不得出现——反证 hybrid 的差异来自向量路
    lexical = _search(make_client(), token)
    assert lexical.json()["scoring"] == "lexical-v1"
    assert dense_only not in _ids(lexical)


def test_success_audit_mode_matches_response(
    make_client: Any, admin: psycopg.Connection[Any], tenant: str
) -> None:
    version = _version(admin, tenant)
    _chunk(admin, tenant, version, create_acl(admin, tenant, {"allow": {"roles": ["staff"]}}))
    _, token = _staff(admin, tenant)
    admin.commit()

    response = _search(make_client(retrieval_mode="hybrid", embedding_provider="test"), token)
    assert response.status_code == 200
    trace_id = response.json()["trace_id"]
    rows = [row for row in _audit_rows(admin, tenant) if row[3] == trace_id]
    assert len(rows) == 1
    assert rows[0][0] == "SUCCEEDED"
    assert rows[0][4] == response.json()["scoring"] == "hybrid-rrf-v1"


# ---------------------------------------------------------------------------
# Q2：缺模型 / 配置错误一律 fail-closed，不伪装成功
# ---------------------------------------------------------------------------
def test_hybrid_without_model_is_503_and_audits_not_executed(
    make_client: Any, admin: psycopg.Connection[Any], tenant: str
) -> None:
    _, token = _staff(admin, tenant)
    admin.commit()

    response = _search(make_client(retrieval_mode="hybrid", embedding_provider="none"), token)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "SERVICE_UNAVAILABLE"
    assert "results" not in response.json()

    rows = _audit_rows(admin, tenant)
    assert [(row[0], row[1], row[4]) for row in rows] == [
        ("ERROR", ERROR_EMBEDDING_UNAVAILABLE, "not_executed")
    ]


def test_unknown_mode_is_503_and_never_falls_back_to_lexical(
    make_client: Any, admin: psycopg.Connection[Any], tenant: str
) -> None:
    version = _version(admin, tenant)
    _chunk(admin, tenant, version, create_acl(admin, tenant, {"allow": {"roles": ["staff"]}}))
    _, token = _staff(admin, tenant)
    admin.commit()

    response = _search(make_client(retrieval_mode="semantic"), token)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "SERVICE_UNAVAILABLE"

    rows = _audit_rows(admin, tenant)
    assert [(row[0], row[4]) for row in rows] == [("ERROR", "not_executed")]


def test_production_env_rejects_test_embedder(
    make_client: Any, admin: psycopg.Connection[Any], tenant: str
) -> None:
    with pytest.raises(EmbeddingNotConfigured):
        build_embedder("test", env="prod")

    _, token = _staff(admin, tenant)
    admin.commit()
    response = _search(
        make_client(env="prod", retrieval_mode="hybrid", embedding_provider="test"), token
    )
    assert response.status_code == 503


def test_lexical_mode_never_loads_embedding_dependencies(
    make_client: Any, admin: psycopg.Connection[Any], tenant: str
) -> None:
    version = _version(admin, tenant)
    chunk = _chunk(admin, tenant, version, create_acl(admin, tenant, {"allow": {"roles": ["staff"]}}))
    _, token = _staff(admin, tenant)
    admin.commit()

    try:
        import sentence_transformers  # noqa: F401
    except ImportError:
        with pytest.raises(EmbeddingNotConfigured):
            get_embedder("bge-m3")

    # 配了 bge-m3 但模式是词法：不得尝试加载模型，请求必须正常
    response = _search(make_client(retrieval_mode="lexical", embedding_provider="bge-m3"), token)
    assert response.status_code == 200
    assert response.json()["scoring"] == "lexical-v1"
    assert chunk in _ids(response)


# ---------------------------------------------------------------------------
# Q4：模型隔离与融合后 ACL
# ---------------------------------------------------------------------------
def test_vectors_from_other_model_are_ignored(
    make_client: Any, admin: psycopg.Connection[Any], tenant: str
) -> None:
    embedder = DeterministicTestEmbedder()
    version = _version(admin, tenant)
    acl = create_acl(admin, tenant, {"allow": {"roles": ["staff"]}})
    stale = _chunk(
        admin,
        tenant,
        version,
        acl,
        text=DENSE_ONLY_TEXT,
        embedding=embedder.embed_query("冷凝管规格"),
        embedding_model_id="other-model-v9",
    )
    _, token = _staff(admin, tenant)
    admin.commit()

    response = _search(make_client(retrieval_mode="hybrid", embedding_provider="test"), token)
    assert response.status_code == 200
    assert stale not in _ids(response), "别的模型产出的向量不得参与召回"

    # 换成当前模型后同一块才应被向量路召回
    admin.execute(
        "UPDATE knowledge_chunks SET embedding_model_id = %s WHERE id = %s",
        (embedder.model_id, stale),
    )
    admin.commit()
    response = _search(make_client(retrieval_mode="hybrid", embedding_provider="test"), token)
    assert stale in _ids(response)


def test_hybrid_applies_acl_after_fusion(
    make_client: Any, admin: psycopg.Connection[Any], tenant: str
) -> None:
    embedder = DeterministicTestEmbedder()
    version = _version(admin, tenant)
    denied = _chunk(
        admin,
        tenant,
        version,
        create_acl(admin, tenant, {"allow": {"roles": ["manager"]}}),
        embedding=embedder.embed_query("冷凝管规格"),
        embedding_model_id=embedder.model_id,
    )
    principal, _ = _staff(admin, tenant)
    admin.commit()

    guest_token = _token(tenant, principal, roles=("staff",))  # staff 不等于 manager
    response = _search(make_client(retrieval_mode="hybrid", embedding_provider="test"), guest_token)
    assert response.status_code == 200
    assert response.json()["results"] == []
    assert denied not in json.dumps(response.json())


def test_hybrid_dedupes_and_respects_max_results(
    make_client: Any, admin: psycopg.Connection[Any], tenant: str
) -> None:
    embedder = DeterministicTestEmbedder()
    version = _version(admin, tenant)
    acl = create_acl(admin, tenant, {"allow": {"roles": ["staff"]}})
    for _ in range(5):
        _chunk(
            admin,
            tenant,
            version,
            acl,
            embedding=embedder.embed_query(TEXT),
            embedding_model_id=embedder.model_id,
        )
    _, token = _staff(admin, tenant)
    admin.commit()

    response = _search(
        make_client(retrieval_mode="hybrid", embedding_provider="test"), token, max_results=2
    )
    assert response.status_code == 200
    ids = [item["chunk_id"] for item in response.json()["results"]]
    assert len(ids) == len(set(ids)) <= 2, "融合后不得出现重复，也不得超上限"


def test_hybrid_hides_revoked_and_unapproved_versions(
    make_client: Any, admin: psycopg.Connection[Any], tenant: str
) -> None:
    embedder = DeterministicTestEmbedder()
    acl = create_acl(admin, tenant, {"allow": {"roles": ["staff"]}})
    approved = _version(admin, tenant)
    approved_chunk = _chunk(
        admin,
        tenant,
        approved,
        acl,
        embedding=embedder.embed_query(TEXT),
        embedding_model_id=embedder.model_id,
    )
    draft = _version(admin, tenant, status="DRAFT")
    draft_chunk = _chunk(
        admin,
        tenant,
        draft,
        create_acl(admin, tenant, {"allow": {"roles": ["staff"]}}),
        embedding=embedder.embed_query(TEXT),
        embedding_model_id=embedder.model_id,
    )
    _, token = _staff(admin, tenant)
    admin.commit()

    response = _search(make_client(retrieval_mode="hybrid", embedding_provider="test"), token)
    found = _ids(response)
    assert approved_chunk in found
    assert draft_chunk not in found, "非白名单状态不得因向量命中而泄露"


# ---------------------------------------------------------------------------
# Q5：深度上限与融合
# ---------------------------------------------------------------------------
def test_rrf_default_k_is_sixty() -> None:
    assert inspect.signature(reciprocal_rank_fusion).parameters["k"].default == 60


def test_dense_candidate_cap_follows_formula(
    app_conn: psycopg.Connection[Any], tenant: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: list[int] = []

    class _FakeDense:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def search_ids(self, query: str, *, knowledge_scope: str, limit: int) -> list[Any]:
            captured.append(limit)
            return []

    monkeypatch.setattr(hybrid_module, "DenseRetriever", _FakeDense)
    retriever = HybridRetriever(
        app_conn, uuid.UUID(tenant), embedder=DeterministicTestEmbedder()
    )
    from fkh.authz.policy import Subject

    subject = Subject(principal_id=str(uuid.uuid4()), tenant_id=tenant, roles=frozenset({"staff"}))
    retriever.search("冷凝", knowledge_scope="internal_product", subject=subject, max_results=3)
    retriever.search("冷凝", knowledge_scope="internal_product", subject=subject, max_results=60)
    assert captured == [
        min(3 * OVERFETCH, MAX_CANDIDATES),
        min(60 * OVERFETCH, MAX_CANDIDATES),
    ]
    assert captured[1] == 200


def test_hybrid_fuses_when_dense_side_is_empty(
    make_client: Any, admin: psycopg.Connection[Any], tenant: str
) -> None:
    """没有写向量的库（dense 空）仍要能靠词法返回，不能因为一路空就整体失败。"""
    version = _version(admin, tenant)
    chunk = _chunk(admin, tenant, version, create_acl(admin, tenant, {"allow": {"roles": ["staff"]}}))
    _, token = _staff(admin, tenant)
    admin.commit()

    response = _search(make_client(retrieval_mode="hybrid", embedding_provider="test"), token)
    assert response.status_code == 200
    assert response.json()["scoring"] == "hybrid-rrf-v1"
    assert chunk in _ids(response)


# ---------------------------------------------------------------------------
# Q2 / Q6：推理失败与审计失败都不得伪装成功
# ---------------------------------------------------------------------------
def test_inference_failure_is_503_not_fake_success(
    make_client: Any,
    admin: psycopg.Connection[Any],
    tenant: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _BrokenEmbedder:
        model_id = "broken-model"
        dimension = 1024

        def embed_documents(self, texts: Any) -> Any:
            raise EmbeddingError("权重缺失")

        def embed_query(self, text: str) -> list[float]:
            raise EmbeddingError("权重缺失")

    import fkh.api.routes_search as routes_search

    monkeypatch.setattr(routes_search, "get_embedder", lambda *a, **k: _BrokenEmbedder())
    _, token = _staff(admin, tenant)
    admin.commit()

    response = _search(make_client(retrieval_mode="hybrid", embedding_provider="test"), token)
    assert response.status_code == 503
    rows = _audit_rows(admin, tenant)
    assert [(row[0], row[1], row[4]) for row in rows] == [
        ("ERROR", ERROR_EMBEDDING_UNAVAILABLE, "hybrid-rrf-v1")
    ]


def test_audit_failure_blocks_hybrid_results(
    make_client: Any, admin: psycopg.Connection[Any], tenant: str
) -> None:
    version = _version(admin, tenant)
    chunk = _chunk(admin, tenant, version, create_acl(admin, tenant, {"allow": {"roles": ["staff"]}}))
    _, token = _staff(admin, tenant)
    admin.commit()

    admin.execute(
        """CREATE OR REPLACE FUNCTION fkh_test_block_hybrid_audit() RETURNS trigger
           LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'audit blocked'; END; $$;"""
    )
    admin.execute(
        """CREATE TRIGGER trg_test_block_hybrid_audit BEFORE INSERT ON retrieval_queries
           FOR EACH ROW EXECUTE FUNCTION fkh_test_block_hybrid_audit();"""
    )
    admin.commit()
    try:
        response = _search(make_client(retrieval_mode="hybrid", embedding_provider="test"), token)
        assert response.status_code >= 500
        assert chunk not in json.dumps(response.json())
    finally:
        admin.execute("DROP TRIGGER IF EXISTS trg_test_block_hybrid_audit ON retrieval_queries")
        admin.execute("DROP FUNCTION IF EXISTS fkh_test_block_hybrid_audit()")
        admin.commit()


# ---------------------------------------------------------------------------
# Q7：context 固定词法
# ---------------------------------------------------------------------------
def test_context_endpoint_stays_lexical_even_when_search_is_hybrid(
    make_client: Any, admin: psycopg.Connection[Any], tenant: str
) -> None:
    embedder = DeterministicTestEmbedder()
    version = _version(admin, tenant)
    acl = create_acl(admin, tenant, {"allow": {"roles": ["staff"]}})
    lexical_block = _chunk(admin, tenant, version, acl)
    dense_only = _chunk(
        admin,
        tenant,
        version,
        acl,
        text=DENSE_ONLY_TEXT,
        embedding=embedder.embed_query("冷凝管规格"),
        embedding_model_id=embedder.model_id,
    )
    _, token = _staff(admin, tenant)
    admin.commit()

    client = make_client(retrieval_mode="hybrid", embedding_provider="test")
    response = client.post(
        "/v1/knowledge/context",
        json={"query": "冷凝管规格", "knowledge_scope": "internal_product"},
        headers=_auth(token),
    )
    assert response.status_code == 200
    block_ids = {block["chunk_id"] for block in response.json()["blocks"]}
    assert lexical_block in block_ids
    assert dense_only not in block_ids, "context 不得跟随 search 的混合模式"

    rows = [row for row in _audit_rows(admin, tenant) if row[3] == response.json()["trace_id"]]
    assert [(row[0], row[4]) for row in rows] == [("SUCCEEDED", "lexical-v1")]
