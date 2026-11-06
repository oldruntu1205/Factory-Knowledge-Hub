"""词法排序（BM25，lexical-v3）回归：判别词要能排上来，统计要遵守可见性。"""

from __future__ import annotations

import uuid
from typing import Any
from uuid import UUID

import psycopg
import pytest
from tests.conftest import create_principal, create_tenant

from fkh.authz.policy import AclPolicy, Subject
from fkh.domain.review import ReviewService
from fkh.retrieval.lexical import MAX_CANDIDATES, LexicalRetriever
from fkh.workers.ingestion import IngestionRequest, ingest_document

pytestmark = pytest.mark.requires_pg

BOILERPLATE = """## 第 {page} 页

冷凝管 标准口

壁厚3mm，高硼硅精选管材，车床焊接工艺，冷凝管端正结实耐用。
冷凝管的标口从14-70mm，长度均可接受定制，以下规格均为常用规格。
货号 规格 管径*总长 上标口*下标口 单价
A{page}2420 200mm 38*300 24*24 39.00
"""

TUBE = """## 第 76 页

高硼硅试管

试管均为3.3高硼硅玻璃制成，加厚，具有更高的光洁度、平整度、耐高温和耐腐蚀。
货号 直径*长度 壁厚
C190010 10ml 1.2
C190020 20ml 1.4
"""


@pytest.fixture
def tenant(admin: psycopg.Connection[Any]) -> str:
    tenant_id = create_tenant(admin, f"排序 {uuid.uuid4().hex[:6]}")
    admin.commit()
    return tenant_id


def _seed(
    admin: psycopg.Connection[Any],
    conn: psycopg.Connection[Any],
    tenant: str,
    *,
    extra_boilerplate: int = 10,
) -> None:
    actor = create_principal(admin, tenant, f"lib-{uuid.uuid4().hex[:6]}@example.test")
    admin.commit()
    body = "\n\n".join(BOILERPLATE.format(page=i) for i in range(extra_boilerplate)) + "\n\n" + TUBE
    request = IngestionRequest(
        source_id=f"rank-{uuid.uuid4().hex[:8]}",
        filename="catalog.md",
        content=body.encode(),
        knowledge_key=f"rank-{uuid.uuid4().hex[:6]}",
        title="目录",
        domain="product_catalog",
        owner_department="sales",
        classification=2,
        audience="internal",
        required_scope="internal_product",
        acl_policy=AclPolicy.from_json({"allow": {"roles": ["staff"]}, "deny": {}}),
        actor_id=UUID(actor),
        catalog_currency="CNY",
    )
    result = ingest_document(conn, UUID(tenant), request)
    review = ReviewService(conn, UUID(tenant))
    review.pass_review(result.version_id, UUID(actor))
    review.approve_internal(result.version_id, UUID(actor))
    conn.commit()


def _search(conn: psycopg.Connection[Any], tenant: str, query: str, **kwargs: Any) -> Any:
    retriever = LexicalRetriever(conn, UUID(tenant))
    subject = Subject(
        principal_id=str(uuid.uuid4()), tenant_id=tenant, roles=frozenset({"staff"})
    )
    return retriever.search(
        query, knowledge_scope="internal_product", subject=subject, **kwargs
    )


def test_discriminative_term_ranks_over_boilerplate(
    admin: psycopg.Connection[Any], app_conn: psycopg.Connection[Any], tenant: str
) -> None:
    """判别词「试管」必须压过重复样板段落（这正是 BM25 要修的）。"""
    _seed(admin, app_conn, tenant)
    outcome = _search(app_conn, tenant, "有哪些规格的试管？", max_results=5)
    top = [hit.text_snippet for hit in outcome.hits[:3]]
    assert any("试管" in snippet for snippet in top), f"top-3 未见试管块：{top}"


def test_boilerplate_query_still_ranks_condenser_first(
    admin: psycopg.Connection[Any], app_conn: psycopg.Connection[Any], tenant: str
) -> None:
    _seed(admin, app_conn, tenant)
    outcome = _search(app_conn, tenant, "冷凝管", max_results=5)
    assert "冷凝管" in outcome.hits[0].text_snippet


def test_empty_and_punctuation_queries_return_empty(
    admin: psycopg.Connection[Any], app_conn: psycopg.Connection[Any], tenant: str
) -> None:
    _seed(admin, app_conn, tenant)
    assert _search(app_conn, tenant, "   ").hits == ()
    assert _search(app_conn, tenant, "？。！").hits == ()


def test_max_results_above_window_is_rejected(
    admin: psycopg.Connection[Any], app_conn: psycopg.Connection[Any], tenant: str
) -> None:
    _seed(admin, app_conn, tenant)
    with pytest.raises(ValueError, match="候选窗口上限"):
        _search(app_conn, tenant, "冷凝管", max_results=MAX_CANDIDATES + 1)


def test_invisible_chunks_do_not_change_stats_or_ranking(
    admin: psycopg.Connection[Any], app_conn: psycopg.Connection[Any], tenant: str
) -> None:
    """不可见（DRAFT）块不得进入 N/df/avg_len，也不得改变可见结果的排序。"""
    _seed(admin, app_conn, tenant)
    before = _search(app_conn, tenant, "有哪些规格的试管？", max_results=3)

    actor = create_principal(admin, tenant, f"d-{uuid.uuid4().hex[:6]}@example.test")
    admin.commit()
    hidden = "\n\n".join(
        f"试管 隐藏资料 {i} 试 管 试管" for i in range(30)
    )
    ingest_document(
        app_conn,
        UUID(tenant),
        IngestionRequest(
            source_id=f"hidden-{uuid.uuid4().hex[:8]}",
            filename="hidden.md",
            content=hidden.encode(),
            knowledge_key=f"hidden-{uuid.uuid4().hex[:6]}",
            title="隐藏",
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
    app_conn.commit()  # 仍是 DRAFT，对检索不可见

    after = _search(app_conn, tenant, "有哪些规格的试管？", max_results=3)
    assert [hit.chunk_id for hit in after.hits] == [hit.chunk_id for hit in before.hits]
