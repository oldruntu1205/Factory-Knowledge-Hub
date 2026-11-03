"""入库端到端：解析 → 切片 → 落库 → 草稿态。

用真实 PostgreSQL 验证，不 mock。这一层最容易出问题的地方是事务与 RLS 的交互：
租户上下文一旦没设，写入会表现为"成功但零行"，而不是报错。
"""

from __future__ import annotations

import uuid
from typing import Any
from uuid import UUID

import psycopg
import pytest
from tests.conftest import set_tenant

from fkh.authz.policy import AclPolicy
from fkh.domain.lexeme import segment_cjk
from fkh.domain.state_machine import VersionStatus
from fkh.workers.ingestion import IngestionRequest, ingest_document
from fkh.workers.repository import DuplicateContentError

pytestmark = pytest.mark.requires_pg

SENTENCE = "高硼硅玻璃冷凝管适用于回流与蒸馏，接口规格需按客户设备确认。"
CONTENT = f"""# 冷凝管产品说明

{SENTENCE * 12}

## 规格表

| 型号 | 接口 | 长度 |
| --- | --- | --- |
| CG-001 | 24/29 | 300 |
| CG-002 | 29/32 | 400 |

## 使用限制

{SENTENCE * 8}
"""


def _request(seed: dict[str, str], **overrides: Any) -> IngestionRequest:
    payload: dict[str, Any] = {
        "source_id": f"src-{uuid.uuid4().hex[:8]}",
        "filename": "catalog.md",
        "content": CONTENT.encode(),
        "knowledge_key": f"catalog-{uuid.uuid4().hex[:8]}",
        "title": "冷凝管产品说明",
        "domain": "product_catalog",
        "owner_department": "tech",
        "classification": 1,
        "audience": "internal",
        "required_scope": "internal_product",
        "acl_policy": AclPolicy.from_json({"allow": {"roles": ["staff"]}}),
        "actor_id": UUID(seed["user_a"]),
    }
    payload.update(overrides)
    return IngestionRequest(**payload)


def test_ingest_creates_draft_version_with_chunks(
    app_conn: psycopg.Connection[Any], admin: psycopg.Connection[Any], seed: dict[str, str]
) -> None:
    result = ingest_document(app_conn, UUID(seed["tenant_a"]), _request(seed))
    app_conn.commit()

    assert result.status is VersionStatus.DRAFT
    assert result.parent_count > 0
    assert result.child_count >= result.parent_count
    assert result.index_generation >= 1

    row = admin.execute(
        "SELECT status, version_no, classification FROM knowledge_versions WHERE id = %s",
        (result.version_id,),
    ).fetchone()
    assert row is not None
    assert row[0] == "DRAFT"
    assert row[1] == result.version_no

    counts = admin.execute(
        """SELECT chunk_kind, count(*) FROM knowledge_chunks
            WHERE version_id = %s GROUP BY chunk_kind""",
        (result.version_id,),
    ).fetchall()
    by_kind = {kind: count for kind, count in counts}
    assert by_kind.get("parent") == result.parent_count
    assert by_kind.get("child") == result.child_count


def test_ingest_records_submit_audit(
    app_conn: psycopg.Connection[Any], admin: psycopg.Connection[Any], seed: dict[str, str]
) -> None:
    result = ingest_document(app_conn, UUID(seed["tenant_a"]), _request(seed))
    app_conn.commit()

    row = admin.execute(
        """SELECT action, actor_id, reason FROM review_actions WHERE version_id = %s""",
        (result.version_id,),
    ).fetchone()
    assert row is not None
    assert row[0] == "SUBMIT"
    assert str(row[1]) == seed["user_a"]
    assert "ingest" in (row[2] or "")


def test_children_reference_parents_of_same_version(
    app_conn: psycopg.Connection[Any], admin: psycopg.Connection[Any], seed: dict[str, str]
) -> None:
    result = ingest_document(app_conn, UUID(seed["tenant_a"]), _request(seed))
    app_conn.commit()

    orphans = admin.execute(
        """SELECT count(*) FROM knowledge_chunks child
            WHERE child.version_id = %s
              AND child.chunk_kind = 'child'
              AND (child.parent_chunk_id IS NULL
                   OR NOT EXISTS (SELECT 1 FROM knowledge_chunks parent
                                   WHERE parent.id = child.parent_chunk_id
                                     AND parent.chunk_kind = 'parent'
                                     AND parent.version_id = child.version_id))""",
        (result.version_id,),
    ).fetchone()
    assert orphans is not None
    assert orphans[0] == 0


def test_chunks_carry_heading_path_and_locator(
    app_conn: psycopg.Connection[Any], admin: psycopg.Connection[Any], seed: dict[str, str]
) -> None:
    result = ingest_document(app_conn, UUID(seed["tenant_a"]), _request(seed))
    app_conn.commit()

    rows = admin.execute(
        "SELECT heading_path, locator, chunk_text FROM knowledge_chunks WHERE version_id = %s",
        (result.version_id,),
    ).fetchall()
    assert rows
    for heading_path, locator, text in rows:
        assert "block_index" in locator
        if "冷凝管" in text:
            assert heading_path, "含正文关键字的切片必须带标题路径"


def test_chinese_full_text_search_hits(
    app_conn: psycopg.Connection[Any], admin: psycopg.Connection[Any], seed: dict[str, str]
) -> None:
    """字级分词必须让中文关键词能命中，否则 lexeme 等于没写。"""
    result = ingest_document(app_conn, UUID(seed["tenant_a"]), _request(seed))
    app_conn.commit()

    row = admin.execute(
        """SELECT count(*) FROM knowledge_chunks
            WHERE version_id = %s
              AND lexeme @@ plainto_tsquery('simple', %s)""",
        (result.version_id, segment_cjk("冷凝管")),
    ).fetchone()
    assert row is not None
    assert row[0] > 0

    miss = admin.execute(
        """SELECT count(*) FROM knowledge_chunks
            WHERE version_id = %s
              AND lexeme @@ plainto_tsquery('simple', %s)""",
        (result.version_id, segment_cjk("离心泵")),
    ).fetchone()
    assert miss is not None
    assert miss[0] == 0


def test_duplicate_content_rejected(
    app_conn: psycopg.Connection[Any], admin: psycopg.Connection[Any], seed: dict[str, str]
) -> None:
    """同一文档重复上传相同内容必须拒绝，且不产生新版本。"""
    request = _request(seed)
    first = ingest_document(app_conn, UUID(seed["tenant_a"]), request)
    app_conn.commit()

    with pytest.raises(DuplicateContentError):
        ingest_document(
            app_conn,
            UUID(seed["tenant_a"]),
            _request(seed, knowledge_key=request.knowledge_key, source_id="src-again"),
        )
    app_conn.rollback()

    row = admin.execute(
        "SELECT count(*) FROM knowledge_versions WHERE document_id = %s",
        (first.document_id,),
    ).fetchone()
    assert row is not None
    assert row[0] == 1


def test_version_number_increments_for_new_content(
    app_conn: psycopg.Connection[Any], seed: dict[str, str]
) -> None:
    request = _request(seed)
    first = ingest_document(app_conn, UUID(seed["tenant_a"]), request)
    app_conn.commit()

    second = ingest_document(
        app_conn,
        UUID(seed["tenant_a"]),
        _request(
            seed,
            knowledge_key=request.knowledge_key,
            content=(CONTENT + "\n\n补充：壁厚公差 ±0.2 mm。\n").encode(),
        ),
    )
    app_conn.commit()

    assert second.document_id == first.document_id
    assert second.version_no == first.version_no + 1
    assert second.index_generation > first.index_generation


def test_other_tenant_cannot_see_chunks(
    app_conn: psycopg.Connection[Any], seed: dict[str, str]
) -> None:
    result = ingest_document(app_conn, UUID(seed["tenant_a"]), _request(seed))
    app_conn.commit()

    set_tenant(app_conn, seed["tenant_b"])
    row = app_conn.execute(
        "SELECT count(*) FROM knowledge_chunks WHERE version_id = %s", (result.version_id,)
    ).fetchone()
    assert row is not None
    assert row[0] == 0

    set_tenant(app_conn, seed["tenant_a"])
    visible = app_conn.execute(
        "SELECT count(*) FROM knowledge_chunks WHERE version_id = %s", (result.version_id,)
    ).fetchone()
    assert visible is not None
    assert visible[0] > 0


def test_review_reasons_are_persisted(
    app_conn: psycopg.Connection[Any], admin: psycopg.Connection[Any], seed: dict[str, str]
) -> None:
    """解析器标记的复核原因必须进入审计，否则审核人不知道为什么要多看一眼。"""
    content = b"24/29,300,4.2\n29/32,400,5.1\n"
    result = ingest_document(
        app_conn,
        UUID(seed["tenant_a"]),
        _request(seed, filename="spec.csv", content=content, title="无表头参数表"),
    )
    app_conn.commit()

    assert "TABLE_STRUCTURE_UNCERTAIN" in result.review_reasons
    assert result.needs_human_review

    row = admin.execute(
        "SELECT reason FROM review_actions WHERE version_id = %s", (result.version_id,)
    ).fetchone()
    assert row is not None
    assert "TABLE_STRUCTURE_UNCERTAIN" in (row[0] or "")


def test_database_enforces_content_uniqueness(
    admin: psycopg.Connection[Any], seed: dict[str, str]
) -> None:
    """应用层检查可能被并发绕过，唯一性必须由数据库兜底。

    这里绕过 ingest_document 直接插两条同哈希版本，验证约束真实存在。
    """
    document_id = admin.execute(
        """INSERT INTO knowledge_documents (tenant_id, knowledge_key, domain, owner_department)
           VALUES (%s, %s, 'product_catalog', 'tech') RETURNING id""",
        (seed["tenant_a"], f"uniq-{uuid.uuid4().hex[:8]}"),
    ).fetchone()
    assert document_id is not None

    def insert_version(version_no: int) -> None:
        admin.execute(
            """INSERT INTO knowledge_versions
                 (tenant_id, document_id, version_no, title, classification, audience,
                  status, content_sha256, source_object_key)
               VALUES (%s, %s, %s, '重复内容', 1, 'internal', 'DRAFT', %s, 's3://x')""",
            (seed["tenant_a"], document_id[0], version_no, "b" * 64),
        )

    insert_version(1)
    with pytest.raises(psycopg.errors.UniqueViolation):
        insert_version(2)
