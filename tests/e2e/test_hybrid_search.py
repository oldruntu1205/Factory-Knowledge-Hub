"""混合检索（向量 + 词法 + RRF）。

用确定性伪向量验证**工程链路**：写入、模型隔离、双路召回、融合去重、融合后 ACL。
真实语义相似度要用 BGE-M3 才能测，但那是模型能力问题，不是链路问题——
链路错了，再好的模型也检不出来。
"""

from __future__ import annotations

import uuid
from typing import Any
from uuid import UUID

import psycopg
import pytest
from tests.conftest import create_principal, create_tenant

from fkh.authz.policy import AclPolicy, Subject
from fkh.domain.review import ReviewService
from fkh.retrieval.embedding import DeterministicTestEmbedder, to_pgvector
from fkh.retrieval.hybrid import DenseRetriever, HybridRetriever, reciprocal_rank_fusion
from fkh.workers.ingestion import IngestionRequest, ingest_document

pytestmark = pytest.mark.requires_pg

CONTENT = """# 冷凝管说明

高硼硅玻璃冷凝管适用于回流与蒸馏，标准接口为 24/29。

## 使用限制

不得用于氢氟酸环境，升温速率不超过 3 ℃/min。
"""


@pytest.fixture
def tenant(admin: psycopg.Connection[Any]) -> dict[str, str]:
    tenant_id = create_tenant(admin, f"混合-{uuid.uuid4().hex[:6]}")
    principal_id = create_principal(admin, tenant_id, f"hy-{uuid.uuid4().hex[:6]}@example.test")
    admin.commit()
    return {"tenant_id": tenant_id, "principal_id": principal_id}


def _prepare(
    conn: psycopg.Connection[Any],
    tenant: dict[str, str],
    *,
    embedder: DeterministicTestEmbedder | None,
) -> UUID:
    request = IngestionRequest(
        source_id=f"hy-{uuid.uuid4().hex[:8]}",
        filename="hy.md",
        content=CONTENT.encode(),
        knowledge_key=f"hy-{uuid.uuid4().hex[:8]}",
        title="冷凝管说明",
        domain="product_catalog",
        owner_department="tech",
        classification=1,
        audience="internal",
        required_scope="internal_product",
        acl_policy=AclPolicy.from_json({"allow": {"roles": ["staff"]}}),
        actor_id=UUID(tenant["principal_id"]),
    )
    result = ingest_document(conn, UUID(tenant["tenant_id"]), request, embedder=embedder)
    conn.commit()

    service = ReviewService(conn, UUID(tenant["tenant_id"]))
    service.pass_review(result.version_id, UUID(tenant["principal_id"]))
    service.approve_internal(result.version_id, UUID(tenant["principal_id"]))
    conn.commit()
    return result.version_id


def _subject(tenant: dict[str, str], roles: set[str] | None = None) -> Subject:
    return Subject(
        principal_id=tenant["principal_id"],
        tenant_id=tenant["tenant_id"],
        roles=frozenset(roles or {"staff"}),
    )


# ---------------------------------------------------------------------------
# 纯函数
# ---------------------------------------------------------------------------
def test_rrf_prefers_documents_ranked_by_both_sides() -> None:
    a, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    scores = reciprocal_rank_fusion([[a, b, c], [a, c, b]], k=60)
    assert scores[a] > scores[b]
    assert scores[a] > scores[c]


def test_rrf_rejects_invalid_k() -> None:
    with pytest.raises(ValueError):
        reciprocal_rank_fusion([], k=0)


# ---------------------------------------------------------------------------
# 写入
# ---------------------------------------------------------------------------
def test_embeddings_are_persisted_with_model_id(
    app_conn: psycopg.Connection[Any], admin: psycopg.Connection[Any], tenant: dict[str, str]
) -> None:
    embedder = DeterministicTestEmbedder()
    version_id = _prepare(app_conn, tenant, embedder=embedder)

    row = admin.execute(
        """SELECT count(*), count(embedding), min(embedding_model_id), max(embedding_model_id)
             FROM knowledge_chunks WHERE version_id = %s""",
        (version_id,),
    ).fetchone()
    assert row is not None
    total, with_vector, model_min, model_max = row
    assert total > 0
    assert with_vector == total, "所有切片都应写入向量"
    assert model_min == model_max == embedder.model_id


def test_without_embedder_no_vector_written(
    app_conn: psycopg.Connection[Any], admin: psycopg.Connection[Any], tenant: dict[str, str]
) -> None:
    """没配模型时不写向量，也不写假的 model_id。"""
    version_id = _prepare(app_conn, tenant, embedder=None)
    row = admin.execute(
        "SELECT count(embedding), count(embedding_model_id) FROM knowledge_chunks WHERE version_id = %s",
        (version_id,),
    ).fetchone()
    assert row == (0, 0)


# ---------------------------------------------------------------------------
# 召回
# ---------------------------------------------------------------------------
def test_hybrid_finds_by_vector_when_lexical_misses(
    app_conn: psycopg.Connection[Any], admin: psycopg.Connection[Any], tenant: dict[str, str]
) -> None:
    """用切片原文做查询：词法可能命中，向量必然命中（同文本同向量）。"""
    embedder = DeterministicTestEmbedder()
    version_id = _prepare(app_conn, tenant, embedder=embedder)

    row = admin.execute(
        "SELECT chunk_text FROM knowledge_chunks WHERE version_id = %s AND chunk_kind = 'child' LIMIT 1",
        (version_id,),
    ).fetchone()
    assert row is not None
    text = str(row[0])

    retriever = HybridRetriever(app_conn, UUID(tenant["tenant_id"]), embedder=embedder)
    outcome = retriever.search(
        text, knowledge_scope="internal_product", subject=_subject(tenant)
    )
    assert outcome.hits
    assert outcome.candidates > 0


def test_hybrid_fuses_both_paths_without_duplicates(
    app_conn: psycopg.Connection[Any], tenant: dict[str, str]
) -> None:
    embedder = DeterministicTestEmbedder()
    _prepare(app_conn, tenant, embedder=embedder)

    retriever = HybridRetriever(app_conn, UUID(tenant["tenant_id"]), embedder=embedder)
    outcome = retriever.search(
        "冷凝管", knowledge_scope="internal_product", subject=_subject(tenant)
    )
    ids = [hit.chunk_id for hit in outcome.hits]
    assert len(ids) == len(set(ids)), "融合后不得出现重复切片"


def test_vector_from_other_model_is_ignored(
    app_conn: psycopg.Connection[Any], admin: psycopg.Connection[Any], tenant: dict[str, str]
) -> None:
    """换了模型就不能拿旧向量凑数——召回必须按 model_id 隔离。"""
    embedder = DeterministicTestEmbedder()
    version_id = _prepare(app_conn, tenant, embedder=embedder)

    admin.execute(
        "UPDATE knowledge_chunks SET embedding_model_id = 'other-model-v0' WHERE version_id = %s",
        (version_id,),
    )
    admin.commit()

    # 向量路必须完全失效：不能把别的模型产出的向量当成本模型的
    dense_ids = DenseRetriever(app_conn, UUID(tenant["tenant_id"]), embedder).search_ids(
        "冷凝管", knowledge_scope="internal_product", limit=10
    )
    assert dense_ids == [], "旧模型向量不应被向量路召回"

    # 词法路不受影响，仍然可用——两路是独立的，一路失效不等于整体失效
    retriever = HybridRetriever(app_conn, UUID(tenant["tenant_id"]), embedder=embedder)
    outcome = retriever.search(
        "冷凝管", knowledge_scope="internal_product", subject=_subject(tenant)
    )
    assert outcome.hits, "词法路应仍然命中"


def test_acl_applied_after_fusion(
    app_conn: psycopg.Connection[Any], tenant: dict[str, str]
) -> None:
    embedder = DeterministicTestEmbedder()
    _prepare(app_conn, tenant, embedder=embedder)

    retriever = HybridRetriever(app_conn, UUID(tenant["tenant_id"]), embedder=embedder)
    outcome = retriever.search(
        "冷凝管", knowledge_scope="internal_product", subject=_subject(tenant, {"guest"})
    )
    assert outcome.hits == ()


def test_pgvector_serialisation_format() -> None:
    assert to_pgvector([0.5, -0.25]) == "[0.5000000,-0.2500000]"
