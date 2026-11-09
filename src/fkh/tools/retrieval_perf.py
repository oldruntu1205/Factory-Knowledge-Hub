"""大租户词法检索性能基准（合成数据，不改生产 schema）。

目的：在 1k / 10k / 50k / 100k 切片规模上测 `LexicalRetriever.search` 的
p50/p95，并单独测 BM25 全范围统计（`_corpus_stats`）的耗时，回答一个问题：

    10 万块时，成本到底在候选召回，还是在 N/df/avg_len 的全范围统计？

数据全部是合成文本，不含客户资料；评测库放工作区 `.perf-pgdata`，可随时删除。

用法：
    uv run python -m fkh.tools.retrieval_perf --pgdata .perf-pgdata \
        --scales 1000 10000 50000 100000 --iterations 5 \
        --output _evidence/retrieval_perf_20261109.json
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import statistics
import time
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import psycopg
from psycopg.types.json import Jsonb

from fkh.authz.policy import Subject
from fkh.domain.lexeme import build_query, segment_cjk
from fkh.retrieval.lexical import (
    _CANDIDATE_SQL,
    MAX_CANDIDATES,
    OVERFETCH,
    SCORING,
    TOKENIZER_ID,
    LexicalRetriever,
)
from fkh.retrieval.stats import refresh_tenant

ROOT = Path(__file__).resolve().parents[3]
BOILERPLATE = "以下是常用规格说明，实际尺寸以图纸为准，安装前请核对接口与材质。"
FAMILIES = ("冷凝管", "试管", "烧杯", "培养皿", "分液漏斗", "容量瓶")
QUERIES = (
    "冷凝管的规格和接口是多少",
    "试管有哪些常用尺寸",
    "分液漏斗的材质要求",
    "容量瓶的刻度误差",
    "烧杯耐温差性能",
)


def _load_apply_module() -> Any:
    spec = importlib.util.spec_from_file_location("fkh_perf_apply", ROOT / "migrations" / "apply.py")
    if spec is None or spec.loader is None:
        raise RuntimeError("无法加载 migrations/apply.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _chunk_text(index: int) -> str:
    family = FAMILIES[index % len(FAMILIES)]
    return (
        f"{family} 规格 {index:06d} 货号 X{index:06d}，"
        f"接口 24/29，材质高硼硅玻璃。{BOILERPLATE}"
    )


def _seed_tenant(conn: psycopg.Connection[Any]) -> tuple[str, str, list[str], str]:
    tenant = uuid4()
    principal = uuid4()
    conn.execute("INSERT INTO tenants (id, name) VALUES (%s, %s)", (tenant, f"perf-{tenant.hex[:6]}"))
    conn.execute(
        "INSERT INTO principals (id, tenant_id, external_subject, principal_type)"
        " VALUES (%s, %s, %s, 'user')",
        (principal, tenant, f"perf-{principal.hex[:6]}@example.test"),
    )
    conn.execute(
        "INSERT INTO knowledge_domains (tenant_id, code, label, is_default)"
        " VALUES (%s, 'product_catalog', '产品目录', true) ON CONFLICT DO NOTHING",
        (tenant,),
    )
    conn.execute(
        "INSERT INTO catalog_rules (tenant_id, code_pattern, currencies)"
        " VALUES (%s, '^[A-Z]{1,3}[0-9]{5,8}$', ARRAY['CNY']) ON CONFLICT DO NOTHING",
        (tenant,),
    )
    acl_ids: list[str] = []
    for _i in range(4):
        acl = uuid4()
        conn.execute(
            "INSERT INTO acl_policies (id, tenant_id, policy_json) VALUES (%s, %s, %s)",
            (acl, tenant, Jsonb({"allow": {"roles": ["staff"]}})),
        )
        acl_ids.append(str(acl))
    doc = uuid4()
    conn.execute(
        "INSERT INTO knowledge_documents (id, tenant_id, knowledge_key, domain, owner_department)"
        " VALUES (%s, %s, %s, 'product_catalog', 'engineering')",
        (doc, tenant, f"perf-doc-{doc.hex[:6]}"),
    )
    version = uuid4()
    conn.execute(
        """INSERT INTO knowledge_versions
             (id, tenant_id, document_id, version_no, title, classification, audience, status,
              required_scope, content_sha256, source_object_key)
           VALUES (%s, %s, %s, 1, 'perf synthetic', 1, 'internal', 'APPROVED_INTERNAL',
                   'internal_product', %s, 'perf/synthetic.md')""",
        (version, tenant, doc, "0" * 64),
    )
    conn.commit()
    return str(tenant), str(principal), acl_ids, str(version)


def _copy_chunks(
    conn: psycopg.Connection[Any],
    *,
    tenant: str,
    version: str,
    acl_ids: list[str],
    start: int,
    count: int,
) -> None:
    conn.execute(
        """CREATE TEMP TABLE IF NOT EXISTS perf_stage (
             chunk_text text NOT NULL,
             lexeme_text text NOT NULL,
             chunk_sha256 text NOT NULL,
             acl_policy_id uuid NOT NULL
           ) ON COMMIT DROP"""
    )
    conn.execute("TRUNCATE perf_stage")
    with conn.cursor().copy(
        "COPY perf_stage (chunk_text, lexeme_text, chunk_sha256, acl_policy_id) FROM STDIN"
    ) as copy:
        for index in range(start, start + count):
            text = _chunk_text(index)
            copy.write_row(
                (
                    text,
                    segment_cjk(text),
                    hashlib.sha256(text.encode("utf-8")).hexdigest(),
                    acl_ids[index % len(acl_ids)],
                )
            )
    conn.execute(
        """INSERT INTO knowledge_chunks
             (tenant_id, id, version_id, parent_chunk_id, chunk_kind, chunk_text,
              chunk_sha256, tokenizer_id, lexeme_tokenizer_id, heading_path, locator,
              metadata, acl_policy_id, index_generation, lexeme)
           SELECT %s, gen_random_uuid(), %s, NULL, 'child', s.chunk_text,
                  s.chunk_sha256, 'approx-cjk-v1', 'cjk-char-v1', '{}',
                  '{"block_index":0}'::jsonb, '{}'::jsonb, s.acl_policy_id, 1,
                  to_tsvector('simple', s.lexeme_text)
             FROM perf_stage s""",
        (tenant, version),
    )
    conn.commit()


def _refresh_stats(uri: str, tenant: str) -> dict[str, Any]:
    with psycopg.connect(uri, autocommit=False) as conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        started = time.perf_counter()
        summary = refresh_tenant(conn, UUID(tenant))
        conn.commit()
        summary["seconds"] = round(time.perf_counter() - started, 3)
        return summary


def _pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    pos = max(0, min(len(ordered) - 1, int(round((len(ordered) - 1) * p))))
    return round(ordered[pos], 1)


def _measure(
    retriever: LexicalRetriever,
    subject: Subject,
    *,
    iterations: int,
) -> dict[str, Any]:
    totals: list[float] = []
    stats_times: list[float] = []
    candidate_times: list[float] = []
    hit_counts: list[int] = []
    for query in QUERIES:
        # 预热：排除首次计划缓存
        retriever.search(query, knowledge_scope="internal_product", subject=subject, max_results=10)
        ts_query = build_query(query)
        candidate_params = {
            "ts_query": ts_query,
            "tenant": UUID(subject.tenant_id),
            "scope": "internal_product",
            "internal": True,
            "lexeme_tokenizer": TOKENIZER_ID,
            "limit": min(10 * OVERFETCH, MAX_CANDIDATES),
            "version_filter": None,
        }
        for _ in range(iterations):
            start = time.perf_counter()
            outcome = retriever.search(
                query, knowledge_scope="internal_product", subject=subject, max_results=10
            )
            totals.append((time.perf_counter() - start) * 1000)
            hit_counts.append(len(outcome.hits))

            start = time.perf_counter()
            retriever._conn.execute(_CANDIDATE_SQL, candidate_params).fetchall()  # noqa: SLF001
            candidate_times.append((time.perf_counter() - start) * 1000)

            terms = sorted(
                {token for token in segment_cjk(query).split() if any(ch.isalnum() for ch in token)}
            )
            start = time.perf_counter()
            retriever._corpus_stats(
                knowledge_scope="internal_product", subject=subject, terms=terms  # noqa: SLF001
            )
            stats_times.append((time.perf_counter() - start) * 1000)
    return {
        "queries": len(QUERIES),
        "iterations": iterations,
        "samples": len(totals),
        "p50_ms": _pct(totals, 0.50),
        "p95_ms": _pct(totals, 0.95),
        "max_ms": round(max(totals), 1) if totals else 0.0,
        "candidate_limit": min(10 * OVERFETCH, MAX_CANDIDATES),
        "candidate_p50_ms": _pct(candidate_times, 0.50),
        "candidate_p95_ms": _pct(candidate_times, 0.95),
        "stats_p50_ms": _pct(stats_times, 0.50),
        "stats_p95_ms": _pct(stats_times, 0.95),
        "avg_hits": round(statistics.fmean(hit_counts), 2) if hit_counts else 0.0,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="大租户词法检索性能基准（合成数据）")
    parser.add_argument("--pgdata", default=".perf-pgdata")
    parser.add_argument("--scales", nargs="+", type=int, default=[1000, 10000, 50000, 100000])
    parser.add_argument("--iterations", type=int, default=5)
    parser.add_argument("--refresh-stats", action="store_true",
                        help="每级插入后重建检索统计表（Phase 1 验证）")
    parser.add_argument("--output", default=None)
    args = parser.parse_args(argv)

    from pgserver.postgres_server import get_server

    data_dir = Path(args.pgdata)
    if not data_dir.is_absolute():
        data_dir = ROOT / data_dir
    server = get_server(str(data_dir))
    uri = str(server.get_uri())
    _load_apply_module().main(["--url", uri])

    report: dict[str, Any] = {
        "scales": [],
        "scoring": SCORING,
        "queries": list(QUERIES),
        "synthetic": True,
    }
    with psycopg.connect(uri, autocommit=False) as conn:
        tenant, principal, acl_ids, version = _seed_tenant(conn)
        subject = Subject(
            principal_id=principal,
            tenant_id=tenant,
            roles=frozenset({"staff"}),
            groups=frozenset(),
            departments=frozenset(),
        )
        retriever = LexicalRetriever(conn, UUID(tenant))
        inserted = 0
        for scale in sorted(set(args.scales)):
            if scale < inserted:
                raise SystemExit("scales 必须递增")
            _copy_chunks(
                conn,
                tenant=tenant,
                version=version,
                acl_ids=acl_ids,
                start=inserted,
                count=scale - inserted,
            )
            inserted = scale
            conn.execute("ANALYZE knowledge_chunks")
            conn.execute("ANALYZE knowledge_versions")
            conn.commit()
            if args.refresh_stats:
                result_refresh = _refresh_stats(uri, tenant)
                print(json.dumps({"stats_refresh": result_refresh}, ensure_ascii=False))
            result = _measure(retriever, subject, iterations=args.iterations)
            result["chunks"] = scale
            report["scales"].append(result)
            print(json.dumps(result, ensure_ascii=False))

    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"report written: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
