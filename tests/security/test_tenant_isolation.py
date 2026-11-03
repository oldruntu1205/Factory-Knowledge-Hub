"""跨租户数据隔离（P0）。

L1 §5.3 / §8.4 要求：跨租户 SELECT / UPDATE / DELETE 必须拒绝或零行，
且不得因为相似 ID、相似向量而互读。本文件只验证行级安全这一层；
应用层 Policy Engine 的隔离在后续阶段单独验证。
"""

from __future__ import annotations

import psycopg
import pytest
from tests.conftest import set_tenant

pytestmark = [pytest.mark.security, pytest.mark.requires_pg]


def test_select_returns_only_current_tenant(app_conn: psycopg.Connection, seed: dict[str, str]) -> None:
    set_tenant(app_conn, seed["tenant_a"])
    rows = app_conn.execute("SELECT tenant_id::text FROM knowledge_documents").fetchall()
    assert rows, "种子数据应至少包含一条当前租户文档"
    assert {row[0] for row in rows} == {seed["tenant_a"]}


def test_select_without_context_returns_nothing(app_conn: psycopg.Connection) -> None:
    """未设置租户上下文时必须 fail-closed，而不是放开全部。"""
    count = app_conn.execute("SELECT count(*) FROM knowledge_documents").fetchone()
    assert count is not None
    assert count[0] == 0


def test_select_other_tenant_by_primary_key_is_zero_rows(
    app_conn: psycopg.Connection, seed: dict[str, str]
) -> None:
    """已知对方主键也不能读：这是最容易漏的一类越权。"""
    set_tenant(app_conn, seed["tenant_a"])
    row = app_conn.execute(
        "SELECT count(*) FROM knowledge_documents WHERE id = %s", (seed["doc_b"],)
    ).fetchone()
    assert row is not None
    assert row[0] == 0


def test_update_other_tenant_affects_zero_rows(app_conn: psycopg.Connection, seed: dict[str, str]) -> None:
    set_tenant(app_conn, seed["tenant_a"])
    cur = app_conn.execute(
        "UPDATE knowledge_documents SET knowledge_key = 'hijacked' WHERE id = %s", (seed["doc_b"],)
    )
    assert cur.rowcount == 0


def test_delete_other_tenant_affects_zero_rows(app_conn: psycopg.Connection, seed: dict[str, str]) -> None:
    set_tenant(app_conn, seed["tenant_a"])
    cur = app_conn.execute("DELETE FROM knowledge_documents WHERE id = %s", (seed["doc_b"],))
    assert cur.rowcount == 0


def test_insert_into_other_tenant_is_rejected(app_conn: psycopg.Connection, seed: dict[str, str]) -> None:
    set_tenant(app_conn, seed["tenant_a"])
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        app_conn.execute(
            """INSERT INTO knowledge_documents (tenant_id, knowledge_key, domain, owner_department)
               VALUES (%s, 'planted', 'quality', 'tech')""",
            (seed["tenant_b"],),
        )


def test_tenants_table_also_isolated(app_conn: psycopg.Connection, seed: dict[str, str]) -> None:
    set_tenant(app_conn, seed["tenant_a"])
    rows = app_conn.execute("SELECT id::text FROM tenants").fetchall()
    assert {row[0] for row in rows} == {seed["tenant_a"]}


def test_rag_query_cannot_reference_foreign_principal(
    admin: psycopg.Connection, seed: dict[str, str]
) -> None:
    """[补强-2] 复合外键必须挡住跨租户主体引用。"""
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        admin.execute(
            """INSERT INTO rag_queries (tenant_id, principal_id, query_hash, scope, status, trace_id)
               VALUES (%s, %s, 'hash', 'internal_product', 'ANSWERED', 'trace-x')""",
            (seed["tenant_a"], seed["user_b"]),
        )


def test_citation_cannot_reference_foreign_chunk(
    admin: psycopg.Connection, seed: dict[str, str]
) -> None:
    q = admin.execute(
        """INSERT INTO rag_queries (tenant_id, principal_id, query_hash, scope, status, trace_id)
           VALUES (%s, %s, 'hash-2', 'internal_product', 'ANSWERED', 'trace-y') RETURNING id""",
        (seed["tenant_a"], seed["user_a"]),
    ).fetchone()
    assert q is not None

    from tests.conftest import create_acl, create_chunk, create_document, create_version

    doc_b2 = create_document(admin, seed["tenant_b"], "doc-b2")
    acl_b = create_acl(admin, seed["tenant_b"])
    ver_b = create_version(admin, seed["tenant_b"], doc_b2, status="APPROVED_INTERNAL", approved_by=seed["user_b"])
    chunk_b = create_chunk(admin, seed["tenant_b"], ver_b, acl_b, kind="child")

    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        admin.execute(
            """INSERT INTO rag_citations (tenant_id, query_id, chunk_id, version_id, locator)
               VALUES (%s, %s, %s, %s, '{"page":1}'::jsonb)""",
            (seed["tenant_a"], str(q[0]), chunk_b, ver_b),
        )
