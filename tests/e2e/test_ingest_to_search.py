"""入库到检索的接缝（跨 lane 回归）。

存在理由：入库（lane A）与检索（lane B/C）各自的测试都自造数据，
两边都绿，但把它们接起来可能完全不工作。本文件只用真实链路：
`ingest_document` 写入 → 状态机推进 → `LexicalRetriever` 召回。

曾经断在两处，都由本文件固定：
1. `tokenizer_id`（切片 token 计数版本，approx-cjk-v1）被误当作
   `lexeme_tokenizer_id`（倒排分词器版本，cjk-char-v1）使用 → 过滤恒不成立；
2. `required_scope` 未写入（NULL）→ 检索的等值条件恒不成立。
两者都不报错，只是静默返回空结果。
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
from fkh.retrieval.lexical import LexicalRetriever
from fkh.workers.ingestion import IngestionRequest, ingest_document

pytestmark = pytest.mark.requires_pg

SENTENCE = "高硼硅玻璃冷凝管适用于回流与蒸馏，接口规格 24/29。"
CONTENT = f"# 冷凝管说明\n\n{SENTENCE}\n\n## 使用限制\n\n不得用于氢氟酸环境。\n"


@pytest.fixture
def tenant(admin: psycopg.Connection[Any]) -> dict[str, str]:
    """每个测试一个独立租户。

    用共享的 seed 租户会让"A 不该看到 B"这类断言失效：同租户下前面测试
    已批准的内容会出现在后续查询里，测出来的是数据残留而不是隔离性。
    """
    tenant_id = create_tenant(admin, f"接缝-{uuid.uuid4().hex[:6]}")
    principal_id = create_principal(admin, tenant_id, f"seam-{uuid.uuid4().hex[:6]}@example.test")
    admin.commit()
    return {"tenant_id": tenant_id, "principal_id": principal_id}


def _ingest(
    conn: psycopg.Connection[Any],
    tenant_id: str,
    actor_id: str,
    *,
    scope: str = "internal_product",
    audience: str = "internal",
    domain: str = "product_catalog",
    roles: list[str] | None = None,
) -> UUID:
    request = IngestionRequest(
        source_id=f"seam-{uuid.uuid4().hex[:8]}",
        filename="seam.md",
        content=CONTENT.encode(),
        knowledge_key=f"seam-{uuid.uuid4().hex[:8]}",
        title="冷凝管说明",
        domain=domain,
        owner_department="tech",
        classification=1 if audience == "internal" else 0,
        audience=audience,
        required_scope=scope,
        acl_policy=AclPolicy.from_json({"allow": {"roles": roles or ["staff"]}}),
        actor_id=UUID(actor_id),
    )
    result = ingest_document(conn, UUID(tenant_id), request)
    conn.commit()
    return result.version_id


def _approve(conn: psycopg.Connection[Any], tenant_id: str, version_id: UUID, actor_id: str) -> None:
    service = ReviewService(conn, UUID(tenant_id))
    service.pass_review(version_id, UUID(actor_id))
    service.approve_internal(version_id, UUID(actor_id))
    conn.commit()


def _subject(tenant_id: str, principal_id: str, roles: set[str]) -> Subject:
    return Subject(principal_id=principal_id, tenant_id=tenant_id, roles=frozenset(roles))


def test_ingested_and_approved_version_is_searchable(
    app_conn: psycopg.Connection[Any], tenant: dict[str, str]
) -> None:
    tenant, actor = tenant["tenant_id"], tenant["principal_id"]
    version_id = _ingest(app_conn, tenant, actor)
    _approve(app_conn, tenant, version_id, actor)

    outcome = LexicalRetriever(app_conn, UUID(tenant)).search(
        "冷凝管", knowledge_scope="internal_product", subject=_subject(tenant, actor, {"staff"})
    )
    assert outcome.candidates > 0, "入库后检索候选为零，说明写入与分词/范围条件不一致"
    assert outcome.hits
    assert outcome.hits[0].version_id == version_id
    assert "冷凝管" in outcome.hits[0].text_snippet


def test_draft_version_is_not_searchable(
    app_conn: psycopg.Connection[Any], tenant: dict[str, str]
) -> None:
    """未审核内容不得进入检索结果，无论分词是否命中。"""
    tenant, actor = tenant["tenant_id"], tenant["principal_id"]
    _ingest(app_conn, tenant, actor)  # 不推进状态，停在 DRAFT

    outcome = LexicalRetriever(app_conn, UUID(tenant)).search(
        "冷凝管", knowledge_scope="internal_product", subject=_subject(tenant, actor, {"staff"})
    )
    assert outcome.hits == ()


def test_unrelated_term_returns_nothing(
    app_conn: psycopg.Connection[Any], tenant: dict[str, str]
) -> None:
    tenant, actor = tenant["tenant_id"], tenant["principal_id"]
    version_id = _ingest(app_conn, tenant, actor)
    _approve(app_conn, tenant, version_id, actor)

    outcome = LexicalRetriever(app_conn, UUID(tenant)).search(
        "离心泵", knowledge_scope="internal_product", subject=_subject(tenant, actor, {"staff"})
    )
    assert outcome.hits == ()


def test_role_denied_by_acl_is_filtered(
    app_conn: psycopg.Connection[Any], tenant: dict[str, str]
) -> None:
    """SQL 层放行的候选，仍要被应用层 ACL 挡掉。"""
    tenant, actor = tenant["tenant_id"], tenant["principal_id"]
    version_id = _ingest(app_conn, tenant, actor, roles=["staff"])
    _approve(app_conn, tenant, version_id, actor)

    outcome = LexicalRetriever(app_conn, UUID(tenant)).search(
        "冷凝管", knowledge_scope="internal_product", subject=_subject(tenant, "outsider", {"guest"})
    )
    assert outcome.hits == ()
    assert outcome.candidates > 0, "候选应被召回到，只是被 ACL 过滤——否则测的是 SQL 而不是 ACL"


def test_training_scope_does_not_leak_into_product_scope(
    app_conn: psycopg.Connection[Any], tenant: dict[str, str]
) -> None:
    """培训材料与产品资料共用 internal，靠 required_scope 区分。"""
    tenant, actor = tenant["tenant_id"], tenant["principal_id"]
    version_id = _ingest(app_conn, tenant, actor, scope="internal_training", domain="training")
    _approve(app_conn, tenant, version_id, actor)

    retriever = LexicalRetriever(app_conn, UUID(tenant))
    subject = _subject(tenant, actor, {"staff"})

    assert retriever.search("冷凝管", knowledge_scope="internal_training", subject=subject).hits
    assert retriever.search("冷凝管", knowledge_scope="internal_product", subject=subject).hits == ()


def test_ingestion_rejects_scope_inconsistent_with_audience(
    app_conn: psycopg.Connection[Any], tenant: dict[str, str]
) -> None:
    """audience=internal 却要求 customer_public 范围，是配置错误，必须在构造时就拒。"""
    with pytest.raises(ValueError, match="required_scope"):
        IngestionRequest(
            source_id="bad",
            filename="bad.md",
            content=b"# x\n\ncontent\n",
            knowledge_key="bad",
            title="x",
            domain="product_catalog",
            owner_department="tech",
            classification=1,
            audience="internal",
            required_scope="customer_public",
            acl_policy=AclPolicy.from_json({"allow": {"roles": ["staff"]}}),
            actor_id=UUID(tenant["principal_id"]),
        )
