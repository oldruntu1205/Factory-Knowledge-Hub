"""检索管线的端到端串联。

覆盖除生成以外的全部环节：
入库 → 审批 → 检索 → 上下文组装 → 引用核验 → AnswerResponse。

生成模型尚未接入（ADR-04/06），但"模型给出的引用是否可信"这一环可以先用
构造的 ID 列表验证——这正是防伪造引用的位置。
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any
from uuid import UUID

import psycopg
import pytest
from jsonschema import Draft202012Validator
from tests.conftest import create_principal, create_tenant

from fkh.authz.policy import AclPolicy, Subject
from fkh.domain.review import ReviewService
from fkh.retrieval.citation import build_citations, decide, to_response
from fkh.retrieval.context import assemble_context
from fkh.retrieval.lexical import LexicalRetriever
from fkh.workers.ingestion import IngestionRequest, ingest_document

pytestmark = pytest.mark.requires_pg

SCHEMA = json.loads(
    (Path(__file__).resolve().parents[2] / "contracts" / "schema" / "answer_response.json").read_text(
        encoding="utf-8"
    )
)

CONTENT = """# 冷凝管说明

高硼硅玻璃冷凝管适用于回流与蒸馏，标准接口为 24/29 与 29/32。

## 使用限制

不得用于氢氟酸环境，升温速率建议不超过 3 ℃/min。
"""


@pytest.fixture
def tenant(admin: psycopg.Connection[Any]) -> dict[str, str]:
    tenant_id = create_tenant(admin, f"管线-{uuid.uuid4().hex[:6]}")
    principal_id = create_principal(admin, tenant_id, f"pipe-{uuid.uuid4().hex[:6]}@example.test")
    admin.commit()
    return {"tenant_id": tenant_id, "principal_id": principal_id}


def _prepare(conn: psycopg.Connection[Any], tenant: dict[str, str]) -> UUID:
    request = IngestionRequest(
        source_id=f"pipe-{uuid.uuid4().hex[:8]}",
        filename="pipe.md",
        content=CONTENT.encode(),
        knowledge_key=f"pipe-{uuid.uuid4().hex[:8]}",
        title="冷凝管说明",
        domain="product_catalog",
        owner_department="tech",
        classification=1,
        audience="internal",
        required_scope="internal_product",
        acl_policy=AclPolicy.from_json({"allow": {"roles": ["staff"]}}),
        actor_id=UUID(tenant["principal_id"]),
    )
    result = ingest_document(conn, UUID(tenant["tenant_id"]), request)
    conn.commit()

    service = ReviewService(conn, UUID(tenant["tenant_id"]))
    service.pass_review(result.version_id, UUID(tenant["principal_id"]))
    service.approve_internal(result.version_id, UUID(tenant["principal_id"]))
    conn.commit()
    return result.version_id


def _subject(tenant: dict[str, str]) -> Subject:
    return Subject(
        principal_id=tenant["principal_id"],
        tenant_id=tenant["tenant_id"],
        roles=frozenset({"staff"}),
    )


def test_full_pipeline_produces_valid_answer_response(
    app_conn: psycopg.Connection[Any], admin: psycopg.Connection[Any], tenant: dict[str, str]
) -> None:
    version_id = _prepare(app_conn, tenant)
    subject = _subject(tenant)
    tenant_id = UUID(tenant["tenant_id"])

    hits = LexicalRetriever(app_conn, tenant_id).search(
        "冷凝管", knowledge_scope="internal_product", subject=subject
    ).hits
    assert hits

    assembly = assemble_context(app_conn, tenant_id, hits, subject)
    assert assembly.blocks

    # 模拟模型只能给出 evidence_id，不能自造内容
    proposed = [str(block.chunk_id) for block in assembly.blocks]
    knowledge_map = {str(block.version_id): str(uuid.uuid4()) for block in assembly.blocks}
    build = build_citations(proposed, assembly.blocks, knowledge_id_by_version=knowledge_map)
    plan = decide(assembly.blocks, build, answer_text="冷凝管标准接口为 24/29 与 29/32。")

    assert plan.status.value == "ANSWERED"
    assert plan.refusal_code is None

    payload = to_response(plan, answer_text="冷凝管标准接口为 24/29 与 29/32。", trace_id="trace-pipe", index_generation=1)
    Draft202012Validator(SCHEMA).validate(payload)

    # 引用必须指向真实存在的、已审批版本的切片
    for citation in payload["citations"]:
        row = admin.execute(
            "SELECT version_id FROM knowledge_chunks WHERE id = %s", (citation["evidence_id"],)
        ).fetchone()
        assert row is not None
        assert str(row[0]) == str(version_id)


def test_pipeline_rejects_fabricated_citation(
    app_conn: psycopg.Connection[Any], tenant: dict[str, str]
) -> None:
    _prepare(app_conn, tenant)
    subject = _subject(tenant)
    tenant_id = UUID(tenant["tenant_id"])

    hits = LexicalRetriever(app_conn, tenant_id).search(
        "冷凝管", knowledge_scope="internal_product", subject=subject
    ).hits
    assembly = assemble_context(app_conn, tenant_id, hits, subject)

    fabricated = str(uuid.uuid4())
    build = build_citations(
        [str(assembly.blocks[0].chunk_id), fabricated],
        assembly.blocks,
        knowledge_id_by_version={},
    )
    plan = decide(assembly.blocks, build, answer_text="答案")

    assert plan.status.value == "HUMAN_REVIEW_REQUIRED"
    assert fabricated in build.unknown_ids
    payload = to_response(plan, answer_text="答案", trace_id="trace-fab", index_generation=1)
    Draft202012Validator(SCHEMA).validate(payload)


def test_pipeline_refuses_without_evidence(
    app_conn: psycopg.Connection[Any], tenant: dict[str, str]
) -> None:
    _prepare(app_conn, tenant)
    tenant_id = UUID(tenant["tenant_id"])
    subject = _subject(tenant)

    hits = LexicalRetriever(app_conn, tenant_id).search(
        "离心泵选型", knowledge_scope="internal_product", subject=subject
    ).hits
    assembly = assemble_context(app_conn, tenant_id, hits, subject)

    build = build_citations([], assembly.blocks, knowledge_id_by_version={})
    plan = decide(assembly.blocks, build, answer_text="")

    assert plan.status.value == "REFUSED"
    assert plan.refusal_code is not None
    payload = to_response(plan, answer_text="", trace_id="trace-none", index_generation=1)
    Draft202012Validator(SCHEMA).validate(payload)
