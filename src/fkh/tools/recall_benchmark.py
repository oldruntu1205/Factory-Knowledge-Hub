"""Recall@K 评测：词法召回 vs 混合召回（真实 embedder）。

用途
----
在**真实语料**上比较两种召回模式的 recall@K，为「是否启用混合模式 / 是否达到验收线」
提供可复现的数字。它不是基准榜单，而是同一语料、同一批问题下的相对比较。

判定口径（刻意宽松但可复核）
--------------------------
top-K 里任一命中块的**正文**包含 `case.needle` 即算命中。用「片段是否包含正确答案串」
而不是「chunk_id 是否等于某个固定值」：切片边界会随 chunking 参数变化，绑 chunk_id 的
评测集一改参数就全部失效，且会奖励「切得碎」而不是「能召回」。

用法
----
    uv run python -m fkh.tools.recall_benchmark \
        --corpus docs README.md \
        --cases _evidence/recall_cases_20261104.json \
        --k 10 --pgdata "%TEMP%/fkh-bench-pgdata" --output _evidence/recall_report.json

注意
----
- 评测库用独立数据目录，不碰测试库；建好后可直接删除。
- 需要真实 embedding 依赖（`uv sync --extra embedding`）与本地权重；
  `--embedder test` 只用于验证评测脚本本身，数字没有语义意义。
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import psycopg

from fkh.authz.policy import AclPolicy, Subject
from fkh.domain.review import ReviewService
from fkh.retrieval.embedding import Embedder, build_embedder
from fkh.retrieval.hybrid import HybridRetriever
from fkh.retrieval.lexical import LexicalRetriever
from fkh.workers.ingestion import IngestionRequest, ingest_document

SCOPE = "internal_product"


@dataclass(frozen=True, slots=True)
class BenchmarkCase:
    id: str
    query: str
    needle: str
    # 该事实所在文档（不含扩展名）。用来算「文档级召回」：切片边界把答案切成两半时，
    # 片段级口径会误判未命中，文档级口径只看有没有找回对的那一篇。
    doc: str | None = None


def load_cases(path: str | Path) -> list[BenchmarkCase]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    cases = [
        BenchmarkCase(
            id=str(item["id"]),
            query=str(item["query"]),
            needle=str(item["needle"]),
            doc=str(item["doc"]) if item.get("doc") else None,
        )
        for item in raw["cases"]
    ]
    if not cases:
        raise ValueError("评测集为空")
    for case in cases:
        if not case.query.strip() or not case.needle.strip():
            raise ValueError(f"用例 {case.id} 的 query/needle 不得为空")
    return cases


def hit_at_k(texts: Sequence[str], needle: str, k: int) -> bool:
    """top-K 命中：任一文本包含 needle。k<=0 视为不命中，避免「空结果算通过」。"""
    if k <= 0:
        return False
    return any(needle in text for text in texts[:k])


def summarize(rows: Sequence[dict[str, Any]], *, k: int = 10) -> dict[str, Any]:
    """汇总：两路 recall@k 与平均耗时。空输入直接报错，不让 0/0 变成 0。"""
    if not rows:
        raise ValueError("没有评测结果")
    total = len(rows)
    lexical = sum(1 for row in rows if row["lexical_hit"])
    hybrid = sum(1 for row in rows if row["hybrid_hit"])
    summary: dict[str, Any] = {
        "cases": total,
        "lexical_recall": round(lexical / total, 4),
        "hybrid_recall": round(hybrid / total, 4),
        "delta": round((hybrid - lexical) / total, 4),
        "lexical_only_hits": sum(1 for row in rows if row["lexical_hit"] and not row["hybrid_hit"]),
        "hybrid_only_hits": sum(1 for row in rows if row["hybrid_hit"] and not row["lexical_hit"]),
        "avg_lexical_ms": round(_avg(row["lexical_ms"] for row in rows), 1),
        "avg_hybrid_ms": round(_avg(row["hybrid_ms"] for row in rows), 1),
    }
    # 精度：P@K 与「实际返回精度」（按文档级相关性代理计算，两路同尺）。
    # 没有相关性字段的行（早期/精简结果）跳过精度段，避免把缺失当成 0。
    if all("lexical_relevant" in row and "hybrid_relevant" in row for row in rows):
        summary["lexical_p_at_k"] = round(
            _avg(precision_at_k(row["lexical_relevant"], k) for row in rows), 4
        )
        summary["hybrid_p_at_k"] = round(
            _avg(precision_at_k(row["hybrid_relevant"], k) for row in rows), 4
        )
        summary["lexical_precision_returned"] = round(
            _avg(
                value
                for row in rows
                if (value := precision_returned(row["lexical_relevant"], row["lexical_returned"]))
                is not None
            ),
            4,
        )
        summary["hybrid_precision_returned"] = round(
            _avg(
                value
                for row in rows
                if (value := precision_returned(row["hybrid_relevant"], row["hybrid_returned"]))
                is not None
            ),
            4,
        )
        summary["worst_hybrid_precision"] = round(
            min(precision_at_k(row["hybrid_relevant"], k) for row in rows), 4
        )

    # 文档级召回：不惩罚切片位置，只看有没有找回正确的那一篇文档
    if all("lexical_doc_hit" in row for row in rows):
        summary["lexical_doc_recall"] = round(
            sum(1 for row in rows if row["lexical_doc_hit"]) / total, 4
        )
        summary["hybrid_doc_recall"] = round(
            sum(1 for row in rows if row["hybrid_doc_hit"]) / total, 4
        )
    return summary


def _relevant_count(docs: Sequence[str], target: str | None, k: int) -> int:
    """文档级相关性代理：命中块是否来自含正确答案的那篇文档。

    这是**代理指标**，不是人工逐条相关性标注：同一篇文档里也可能有与该问题无关的段落，
    跨文档的同义答案会低估，因此不能等同真实相关性精度；
    两路用同一把尺，只做相对比较。
    """
    if not target:
        return 0
    return sum(1 for doc in docs[:k] if doc == target)


def precision_at_k(relevant: int, k: int) -> float:
    """P@K：相关条数 / K，缺位按不相关计（L1 2026-11-04 要求的口径）。"""
    if k <= 0:
        raise ValueError("k 必须为正整数")
    return relevant / k


def precision_returned(relevant: int, returned: int) -> float | None:
    """实际返回精度：相关条数 / 实际返回条数；空结果单列（返回 None 而不是 0）。"""
    if returned <= 0:
        return None
    return relevant / returned


def _avg(values: Iterable[float]) -> float:
    items = list(values)
    return sum(items) / len(items) if items else 0.0


def _load_apply_module() -> Any:
    root = Path(__file__).resolve().parents[3]
    spec = importlib.util.spec_from_file_location("fkh_apply", root / "migrations" / "apply.py")
    if spec is None or spec.loader is None:
        raise RuntimeError("无法加载 migrations/apply.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _collect_files(corpus: Sequence[str]) -> list[Path]:
    files: list[Path] = []
    for entry in corpus:
        path = Path(entry)
        if path.is_dir():
            files.extend(sorted(p for p in path.rglob("*.md") if p.is_file()))
        elif path.is_file():
            files.append(path)
        else:
            raise FileNotFoundError(f"语料路径不存在：{entry}")
    if not files:
        raise ValueError("语料为空")
    return files


def _seed_tenant(conn: psycopg.Connection[Any]) -> tuple[UUID, UUID]:
    tenant = uuid4()
    principal = uuid4()
    conn.execute("INSERT INTO tenants (id, name) VALUES (%s, %s)", (tenant, f"bench-{tenant.hex[:6]}"))
    conn.execute(
        """INSERT INTO principals (id, tenant_id, external_subject, principal_type)
           VALUES (%s, %s, %s, 'user')""",
        (principal, tenant, f"bench-{principal.hex[:6]}@example.test"),
    )
    policy = psycopg.types.json.Jsonb({"allow": {"roles": ["staff"]}})
    conn.execute(
        "INSERT INTO acl_policies (tenant_id, policy_json) VALUES (%s, %s)", (tenant, policy)
    )
    conn.commit()
    return tenant, principal


def _ingest_corpus(
    conn: psycopg.Connection[Any],
    tenant: UUID,
    principal: UUID,
    files: Sequence[Path],
    embedder: Embedder | None,
) -> int:
    acl = AclPolicy.from_json({"allow": {"roles": ["staff"]}})
    done = 0
    for path in files:
        request = IngestionRequest(
            source_id=f"bench-{path.stem}"[:60],
            filename=path.name,
            content=path.read_bytes(),
            knowledge_key=f"bench-{path.stem}-{uuid4().hex[:6]}",
            title=path.stem,
            domain="product_catalog",
            owner_department="tech",
            classification=1,
            audience="internal",
            required_scope=SCOPE,
            acl_policy=acl,
            actor_id=principal,
        )
        result = ingest_document(conn, tenant, request, embedder=embedder)
        ReviewService(conn, tenant).pass_review(result.version_id, principal)
        ReviewService(conn, tenant).approve_internal(result.version_id, principal)
        conn.commit()
        done += 1
    return done


def _docs(conn: psycopg.Connection[Any], ids: Sequence[UUID]) -> list[str]:
    """命中切片所属文档的 stem（bench-<stem>-xxxxx → <stem>）。"""
    if not ids:
        return []
    rows = conn.execute(
        """SELECT ch.id, d.knowledge_key
             FROM knowledge_chunks ch
             JOIN knowledge_versions v ON v.tenant_id = ch.tenant_id AND v.id = ch.version_id
             JOIN knowledge_documents d ON d.tenant_id = v.tenant_id AND d.id = v.document_id
            WHERE ch.id = ANY(%s)""",
        (list(ids),),
    ).fetchall()
    by_id: dict[Any, str] = {}
    for chunk_id, key in rows:
        stem = str(key)
        if stem.startswith("bench-"):
            stem = stem[len("bench-"):]
            stem = stem.rsplit("-", 1)[0]
        by_id[chunk_id] = stem
    return [by_id.get(chunk_id, "") for chunk_id in ids]


def _texts(conn: psycopg.Connection[Any], ids: Sequence[UUID]) -> list[str]:
    if not ids:
        return []
    rows = conn.execute(
        "SELECT id, chunk_text FROM knowledge_chunks WHERE id = ANY(%s)", (list(ids),)
    ).fetchall()
    by_id = {row[0]: str(row[1]) for row in rows}
    return [by_id.get(chunk_id, "") for chunk_id in ids]


def evaluate(
    conn: psycopg.Connection[Any],
    tenant: UUID,
    principal: UUID,
    cases: Sequence[BenchmarkCase],
    embedder: Embedder,
    *,
    k: int,
    rrf_k: int = 60,
) -> list[dict[str, Any]]:
    subject = Subject(
        principal_id=str(principal), tenant_id=str(tenant), roles=frozenset({"staff"})
    )
    rows: list[dict[str, Any]] = []
    for case in cases:
        started = time.monotonic()
        lexical = LexicalRetriever(conn, tenant).search(
            case.query, knowledge_scope=SCOPE, subject=subject, max_results=k
        )
        lexical_ms = (time.monotonic() - started) * 1000
        lexical_ids = [hit.chunk_id for hit in lexical.hits]
        lexical_texts = _texts(conn, lexical_ids)
        lexical_docs = _docs(conn, lexical_ids)

        started = time.monotonic()
        hybrid = HybridRetriever(conn, tenant, embedder=embedder, rrf_k=rrf_k).search(
            case.query, knowledge_scope=SCOPE, subject=subject, max_results=k
        )
        hybrid_ms = (time.monotonic() - started) * 1000
        hybrid_ids = [hit.chunk_id for hit in hybrid.hits]
        hybrid_texts = _texts(conn, hybrid_ids)
        hybrid_docs = _docs(conn, hybrid_ids)

        rows.append(
            {
                "id": case.id,
                "query": case.query,
                "lexical_hit": hit_at_k(lexical_texts, case.needle, k),
                "hybrid_hit": hit_at_k(hybrid_texts, case.needle, k),
                "lexical_ms": lexical_ms,
                "hybrid_ms": hybrid_ms,
                "lexical_top": lexical_texts[:k],
                "hybrid_top": hybrid_texts[:k],
                "doc": case.doc,
                "lexical_returned": len(lexical_texts[:k]),
                "hybrid_returned": len(hybrid_texts[:k]),
                "lexical_relevant": _relevant_count(lexical_docs, case.doc, k),
                "hybrid_relevant": _relevant_count(hybrid_docs, case.doc, k),
                "lexical_doc_hit": bool(case.doc) and case.doc in lexical_docs[:k],
                "hybrid_doc_hit": bool(case.doc) and case.doc in hybrid_docs[:k],
            }
        )
    return rows


def _print_table(summary: dict[str, Any], rows: Sequence[dict[str, Any]]) -> None:
    print(f"用例数：{summary['cases']}  K={summary.get('k', '')}")
    print(
        f"recall  lexical={summary['lexical_recall']:.3f}  "
        f"hybrid={summary['hybrid_recall']:.3f}  delta={summary['delta']:+.3f}"
    )
    if "hybrid_doc_recall" in summary:
        print(
            f"doc@10  lexical={summary['lexical_doc_recall']:.3f}  "
            f"hybrid={summary['hybrid_doc_recall']:.3f}"
        )
    print(
        f"仅词法命中 {summary['lexical_only_hits']} / 仅混合命中 {summary['hybrid_only_hits']}；"
        f"平均耗时 lexical={summary['avg_lexical_ms']}ms hybrid={summary['avg_hybrid_ms']}ms"
    )
    misses = [row for row in rows if not row["hybrid_hit"] and not row["lexical_hit"]]
    if misses:
        print(f"两路都未命中：{len(misses)} 条")
        for row in misses[:10]:
            print(f"  - {row['id']}: {row['query']}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Recall@K 评测（词法 vs 混合）")
    parser.add_argument("--corpus", nargs="+", required=True, help="语料目录或文件（.md）")
    parser.add_argument("--cases", required=True, help="评测集 JSON")
    parser.add_argument("--k", type=int, default=10)
    parser.add_argument("--rrf-k", type=int, default=60)
    parser.add_argument("--pgdata", default=None, help="评测库数据目录（默认系统临时目录下 fkh-bench-pgdata）")
    parser.add_argument("--embedder", default="bge-m3", choices=["bge-m3", "test", "none"])
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--env", default="local", help="embedding provider 的环境闸门")
    parser.add_argument("--output", default=None, help="把逐条结果写成 JSON")
    args = parser.parse_args(argv)

    from pgserver.postgres_server import get_server

    files = _collect_files(args.corpus)
    cases = load_cases(args.cases)
    data_dir = args.pgdata or str(Path.home() / "AppData" / "Local" / "Temp" / "fkh-bench-pgdata")
    server = get_server(data_dir)
    uri = str(server.get_uri())
    _load_apply_module().main(["--url", uri])

    embedder = build_embedder(args.embedder, device=args.device, env=args.env)

    with psycopg.connect(uri, autocommit=False) as conn:
        tenant, principal = _seed_tenant(conn)
        ingested = _ingest_corpus(conn, tenant, principal, files, embedder)
        print(f"已入库 {ingested} 个文档（embedder={args.embedder}）")
        if embedder is None:
            raise SystemExit("--embedder none 无法评测混合模式")
        rows = evaluate(conn, tenant, principal, cases, embedder, k=args.k, rrf_k=args.rrf_k)

    summary = summarize(rows, k=args.k)
    summary["k"] = args.k
    _print_table(summary, rows)
    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(
            json.dumps({"summary": summary, "rows": rows}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"逐条结果已写入 {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
