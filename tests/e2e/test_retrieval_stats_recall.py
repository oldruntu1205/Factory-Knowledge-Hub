"""50 条冻结查询：统计表路径与 live 统计的有序 top-10 必须完全一致。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import psycopg
import pytest

from fkh.authz.policy import Subject
from fkh.retrieval.lexical import LexicalRetriever
from fkh.retrieval.stats import refresh_tenant
from fkh.tools.recall_benchmark import _collect_files, _ingest_corpus, _seed_tenant, load_cases

pytestmark = pytest.mark.requires_pg

ROOT = Path(__file__).resolve().parents[2]
CASES = ROOT / "_evidence" / "recall_cases_20261104.json"


def _top(retriever: LexicalRetriever, subject: Subject, query: str) -> list[tuple[Any, float]]:
    return [
        (hit.chunk_id, round(hit.score, 9))
        for hit in retriever.search(
            query,
            knowledge_scope="internal_product",
            subject=subject,
            max_results=10,
        ).hits
    ]


def test_frozen_50_cases_stats_top10_equals_live(pg_url: str) -> None:
    with psycopg.connect(pg_url, autocommit=False) as conn:
        tenant, principal = _seed_tenant(conn)
        files = _collect_files([str(ROOT / "docs"), str(ROOT / "README.md")])
        _ingest_corpus(conn, tenant, principal, files, None)
        cases = load_cases(CASES)
        subject = Subject(
            principal_id=str(principal),
            tenant_id=str(tenant),
            roles=frozenset({"staff"}),
        )
        retriever = LexicalRetriever(conn, tenant)

        live = [_top(retriever, subject, case.query) for case in cases]
        conn.commit()

        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        refresh_tenant(conn, tenant)
        conn.commit()

        stats = [_top(retriever, subject, case.query) for case in cases]
        assert stats == live

        state = conn.execute(
            "SELECT status, built_revision, source_revision"
            " FROM retrieval_stats_state WHERE tenant_id = %s",
            (tenant,),
        ).fetchone()
        assert state is not None
        assert state[0] == "READY"
        assert state[1] == state[2]
