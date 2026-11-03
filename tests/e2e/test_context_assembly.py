"""父块恢复接线（P0）。

L1 §8.4 的场景：parent 里混了可公开段落和内部段落，命中公开那一条时，
不能因此把整段 parent 拿出来。`authz.parent` 有判定规则，但规则不接线等于没有。

本文件验证「检索命中 → 上下文块」这一段的真实行为，包括数据库取数与去重。
"""

from __future__ import annotations

import hashlib
import uuid
from typing import Any
from uuid import UUID

import psycopg
import pytest
from tests.conftest import create_acl, create_document, create_tenant, create_version

from fkh.authz.policy import Subject
from fkh.retrieval.context import assemble_context
from fkh.retrieval.lexical import SearchHit

pytestmark = pytest.mark.requires_pg


def _chunk(
    admin: psycopg.Connection[Any],
    tenant: str,
    version: str,
    acl: str,
    *,
    kind: str,
    text: str,
    parent: str | None = None,
) -> str:
    row = admin.execute(
        """INSERT INTO knowledge_chunks
             (tenant_id, version_id, parent_chunk_id, chunk_kind, chunk_text, chunk_sha256,
              tokenizer_id, lexeme_tokenizer_id, heading_path, locator, acl_policy_id, index_generation)
           VALUES (%s, %s, %s, %s, %s, %s, 'approx-cjk-v1', 'cjk-char-v1',
                   ARRAY['第 1 章'], '{"block_index": 0}'::jsonb, %s, 1)
           RETURNING id""",
        (tenant, version, parent, kind, text, hashlib.sha256(text.encode()).hexdigest(), acl),
    ).fetchone()
    assert row is not None
    return str(row[0])


def _hit(chunk_id: str, version_id: str, *, score: float = 1.0) -> SearchHit:
    return SearchHit(
        chunk_id=UUID(chunk_id),
        version_id=UUID(version_id),
        document_id=uuid.uuid4(),
        text_snippet="snippet",
        snippet_truncated=False,
        locator=None,
        heading_path=None,
        score=score,
    )


@pytest.fixture
def env(admin: psycopg.Connection[Any]) -> dict[str, Any]:
    tenant = create_tenant(admin, f"上下文-{uuid.uuid4().hex[:6]}")
    principal = admin.execute(
        "INSERT INTO principals (tenant_id, external_subject, principal_type) "
        "VALUES (%s, %s, 'user') RETURNING id",
        (tenant, f"ctx-{uuid.uuid4().hex[:6]}"),
    ).fetchone()
    document = create_document(admin, tenant, f"ctx-{uuid.uuid4().hex[:6]}")
    version = create_version(
        admin, tenant, document, status="APPROVED_INTERNAL", approved_by=str(principal[0])
    )
    admin.execute(
        "UPDATE knowledge_versions SET required_scope = 'internal_product' WHERE id = %s",
        (version,),
    )
    acl_open = create_acl(admin, tenant, {"allow": {"roles": ["staff"]}})
    acl_secret = create_acl(admin, tenant, {"allow": {"roles": ["exec"]}})
    admin.commit()
    return {
        "tenant": tenant,
        "principal": str(principal[0]),
        "version": version,
        "acl_open": acl_open,
        "acl_secret": acl_secret,
        "admin": admin,
    }


def _subject(env: dict[str, Any], roles: set[str]) -> Subject:
    return Subject(
        principal_id=env["principal"],
        tenant_id=env["tenant"],
        roles=frozenset(roles),
    )


def test_parent_restored_when_all_children_visible(
    app_conn: psycopg.Connection[Any], env: dict[str, Any]
) -> None:
    admin = env["admin"]
    parent = _chunk(admin, env["tenant"], env["version"], env["acl_open"], kind="parent", text="父块全文")
    c1 = _chunk(admin, env["tenant"], env["version"], env["acl_open"], kind="child", text="公开段落一", parent=parent)
    _chunk(admin, env["tenant"], env["version"], env["acl_open"], kind="child", text="公开段落二", parent=parent)
    admin.commit()

    result = assemble_context(
        app_conn, UUID(env["tenant"]), [_hit(c1, env["version"])], _subject(env, {"staff"})
    )
    assert result.parent_restores == 1
    assert len(result.blocks) == 1
    assert result.blocks[0].from_parent
    assert result.blocks[0].text == "父块全文"


def test_mixed_parent_falls_back_to_child_text(
    app_conn: psycopg.Connection[Any], env: dict[str, Any]
) -> None:
    """命中公开 child，但同一 parent 下还有仅 exec 可见的段落：只能返回 child 原文。"""
    admin = env["admin"]
    parent = _chunk(admin, env["tenant"], env["version"], env["acl_open"], kind="parent", text="父块含敏感段")
    visible = _chunk(admin, env["tenant"], env["version"], env["acl_open"], kind="child", text="可公开段落", parent=parent)
    _chunk(admin, env["tenant"], env["version"], env["acl_secret"], kind="child", text="仅管理层段落", parent=parent)
    admin.commit()

    result = assemble_context(
        app_conn, UUID(env["tenant"]), [_hit(visible, env["version"])], _subject(env, {"staff"})
    )
    assert result.parent_restores == 0
    assert len(result.blocks) == 1
    assert not result.blocks[0].from_parent
    assert result.blocks[0].text == "可公开段落"
    assert "仅管理层" not in result.blocks[0].text


def test_multiple_hits_in_same_parent_produce_one_block(
    app_conn: psycopg.Connection[Any], env: dict[str, Any]
) -> None:
    admin = env["admin"]
    parent = _chunk(admin, env["tenant"], env["version"], env["acl_open"], kind="parent", text="父块全文")
    c1 = _chunk(admin, env["tenant"], env["version"], env["acl_open"], kind="child", text="段落一", parent=parent)
    c2 = _chunk(admin, env["tenant"], env["version"], env["acl_open"], kind="child", text="段落二", parent=parent)
    admin.commit()

    result = assemble_context(
        app_conn,
        UUID(env["tenant"]),
        [_hit(c1, env["version"], score=2.0), _hit(c2, env["version"], score=1.0)],
        _subject(env, {"staff"}),
    )
    assert len(result.blocks) == 1
    assert result.parent_restores == 1


def test_chunk_without_parent_stays_as_is(
    app_conn: psycopg.Connection[Any], env: dict[str, Any]
) -> None:
    admin = env["admin"]
    lone = _chunk(admin, env["tenant"], env["version"], env["acl_open"], kind="child", text="独立片段")
    admin.commit()

    result = assemble_context(
        app_conn, UUID(env["tenant"]), [_hit(lone, env["version"])], _subject(env, {"staff"})
    )
    assert len(result.blocks) == 1
    assert result.blocks[0].text == "独立片段"
    assert not result.blocks[0].from_parent


def test_denied_subject_gets_nothing(
    app_conn: psycopg.Connection[Any], env: dict[str, Any]
) -> None:
    admin = env["admin"]
    parent = _chunk(admin, env["tenant"], env["version"], env["acl_open"], kind="parent", text="父块")
    child = _chunk(admin, env["tenant"], env["version"], env["acl_open"], kind="child", text="段落", parent=parent)
    admin.commit()

    result = assemble_context(
        app_conn, UUID(env["tenant"]), [_hit(child, env["version"])], _subject(env, {"guest"})
    )
    assert result.blocks == ()
    assert result.denied == 1


def test_broken_policy_is_treated_as_missing(
    app_conn: psycopg.Connection[Any], env: dict[str, Any]
) -> None:
    """策略 JSON 结构损坏等同于没有策略：拒绝，而不是猜一个宽松默认。"""
    admin = env["admin"]
    broken = admin.execute(
        "INSERT INTO acl_policies (tenant_id, policy_json) VALUES (%s, %s) RETURNING id",
        (env["tenant"], psycopg.types.json.Jsonb({"allow": {"role": ["staff"]}})),
    ).fetchone()
    assert broken is not None
    child = _chunk(admin, env["tenant"], env["version"], str(broken[0]), kind="child", text="段落")
    admin.commit()

    result = assemble_context(
        app_conn, UUID(env["tenant"]), [_hit(child, env["version"])], _subject(env, {"staff"})
    )
    assert result.blocks == ()
    assert result.denied == 1


def test_budget_drops_excess_blocks(
    app_conn: psycopg.Connection[Any], env: dict[str, Any]
) -> None:
    admin = env["admin"]
    ids = [
        _chunk(admin, env["tenant"], env["version"], env["acl_open"], kind="child", text=f"片段{i}")
        for i in range(5)
    ]
    admin.commit()

    result = assemble_context(
        app_conn,
        UUID(env["tenant"]),
        [_hit(cid, env["version"], score=float(i)) for i, cid in enumerate(ids)],
        _subject(env, {"staff"}),
        max_blocks=2,
    )
    assert len(result.blocks) == 2
    assert result.dropped == 3


def test_empty_hits_short_circuit(app_conn: psycopg.Connection[Any], env: dict[str, Any]) -> None:
    result = assemble_context(app_conn, UUID(env["tenant"]), [], _subject(env, {"staff"}))
    assert result.blocks == ()
    assert result.denied == 0
