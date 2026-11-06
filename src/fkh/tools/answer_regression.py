"""答案回归评测：把已知错答与代表性正确案例固定成可重跑的验收集。

为什么要它
----------
改提示词 / 换模型 / 调检索参数都会影响答案质量，凭印象判断"好像好了"没有意义。
这个工具把"必须对"和"必须不能出现"写成可判定的断言，每次改动跑一遍就有可比数字。

用法
----
    uv run python -m fkh.tools.answer_regression \
        --pgdata <数据目录> --corpus _corpus/catalog_2023.md \
        --cases _evidence/answer_cases_20261106.json \
        --model qwen3:14b --with-price-cases 10 \
        --output _evidence/answer_regression_report.json

评测集分两类：
- `cases`：手写断言（含"不得出现"的反例，例如把 14-70mm 答成 24*24）；
- `--with-price-cases N`：从语料里自动抽 N 条「货号→单价」做结构化通道用例（含价格，只在本机语料上生成）。
"""

from __future__ import annotations

import argparse
import json
import re
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import psycopg

from fkh.answer import AnswerService
from fkh.authz.policy import AclPolicy, Subject
from fkh.domain.review import ReviewService
from fkh.generation import OllamaGenerator, TextGenerator
from fkh.retrieval.search import SearchContext
from fkh.workers.ingestion import IngestionRequest, ingest_document

ROW = re.compile(r"^([A-Z]\d{6})\s+(\S.*?)\s+(\d+\.\d{2})\s*$", re.MULTILINE)


def evaluate_case(payload: dict[str, Any], expect: dict[str, Any]) -> tuple[bool, list[str]]:
    """判定一条用例。`not` 优先：出现禁止串直接判失败（哪怕 `any` 命中）。

    顺序刻意如此——"不含税"里也含"含税"，先查禁止串才不会把错答判成通过。
    """
    reasons: list[str] = []
    answer = str(payload.get("answer", ""))
    status = str(payload.get("status", ""))
    citations = payload.get("citations") or []

    wanted_status = expect.get("status")
    if wanted_status and status != wanted_status:
        reasons.append(f"status={status}，期望 {wanted_status}")

    for forbidden in expect.get("not") or []:
        if forbidden and forbidden in answer:
            reasons.append(f"出现禁止串「{forbidden}」")

    required_any = expect.get("any") or []
    if required_any and not any(str(item) in answer for item in required_any):
        reasons.append(f"未命中任一必需串 {required_any}")

    if expect.get("require_citations") and not citations:
        reasons.append("没有任何引用")

    return (not reasons, reasons)


def build_price_cases(corpus_text: str, limit: int) -> list[dict[str, Any]]:
    """从语料里抽「货号 规格 单价」行，构成结构化通道用例（needle 必须唯一）。"""
    cases: list[dict[str, Any]] = []
    seen: dict[str, tuple[str, str]] = {}
    for match in ROW.finditer(corpus_text):
        code, spec, price = match.group(1), match.group(2).strip(), match.group(3)
        line = f"{code} {spec} {price}"
        if corpus_text.count(line) != 1:
            continue
        seen.setdefault(code, (line, price))
    for code, (_line, price) in list(seen.items())[:limit]:
        cases.append(
            {
                "id": f"price-{code}",
                "query": f"{code} 的单价是多少？",
                "expect": {
                    "status": "ANSWERED",
                    "any": [price],
                    "require_citations": True,
                },
            }
        )
    return cases


def _ask(
    service: AnswerService,
    *,
    query: str,
    tenant: uuid.UUID,
    principal: uuid.UUID,
    scope: str,
    trace_id: str,
) -> dict[str, Any]:
    subject = Subject(
        principal_id=str(principal), tenant_id=str(tenant), roles=frozenset({"staff"})
    )
    outcome = service.answer(
        query,
        knowledge_scope=scope,
        language="zh-CN",
        max_evidence=5,
        context=SearchContext(
            tenant_id=tenant,
            principal_id=principal,
            subject=subject,
            allowed_scopes=frozenset({scope}),
            trace_id=trace_id,
        ),
    )
    return dict(outcome.payload)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="答案回归评测")
    parser.add_argument("--pgdata", required=True, help="pgserver 数据目录（会新建评测租户）")
    parser.add_argument("--corpus", required=True, help="语料文件（md）")
    parser.add_argument("--cases", default=None, help="手写用例 JSON")
    parser.add_argument("--model", default=None, help="本地模型名；省略则不跑 RAG 段")
    parser.add_argument("--with-price-cases", type=int, default=0)
    parser.add_argument("--output", default=None)
    parser.add_argument("--currency", default="CNY", choices=["CNY"])
    args = parser.parse_args(argv)

    from pgserver.postgres_server import get_server

    cases: list[dict[str, Any]] = []
    if args.cases:
        payload = json.loads(Path(args.cases).read_text(encoding="utf-8"))
        cases.extend(payload.get("cases", []))
    corpus_text = Path(args.corpus).read_text(encoding="utf-8")

    server = get_server(args.pgdata)
    uri = str(server.get_uri())
    from migrations.apply import main as apply_main

    apply_main(["--url", uri])

    with psycopg.connect(uri) as conn:
        tenant_row = conn.execute(
            "INSERT INTO tenants (name) VALUES (%s) RETURNING id", (f"回归 {uuid.uuid4().hex[:6]}",)
        ).fetchone()
        assert tenant_row is not None
        tenant = uuid.UUID(str(tenant_row[0]))
        principal_row = conn.execute(
            """INSERT INTO principals (tenant_id, external_subject, principal_type)
               VALUES (%s, %s, 'user') RETURNING id""",
            (tenant, f"reg-{uuid.uuid4().hex[:6]}@example.test"),
        ).fetchone()
        assert principal_row is not None
        principal = uuid.UUID(str(principal_row[0]))
        conn.commit()
        conn.execute("SELECT set_config('app.tenant_id', %s, true)", (str(tenant),))

        result = ingest_document(
            conn,
            tenant,
            IngestionRequest(
                source_id=f"reg-{uuid.uuid4().hex[:8]}",
                filename="corpus.md",
                content=corpus_text.encode(),
                knowledge_key=f"reg-{uuid.uuid4().hex[:6]}",
                title="回归语料",
                domain="product_catalog",
                owner_department="sales",
                classification=2,
                audience="internal",
                required_scope="internal_product",
                acl_policy=AclPolicy.from_json({"allow": {"roles": ["staff"]}, "deny": {}}),
                actor_id=principal,
                catalog_currency=args.currency,
            ),
        )
        review = ReviewService(conn, tenant)
        review.pass_review(result.version_id, principal)
        review.approve_internal(result.version_id, principal)
        conn.commit()

        if args.with_price_cases:
            # 冲突货号本来就"决议前不可答"，不能算回归失败——直接排除
            conflicted = {
                str(row[0])
                for row in conn.execute(
                    "SELECT code FROM catalog_conflict_groups WHERE tenant_id = %s", (tenant,)
                ).fetchall()
            }
            price_cases = [
                case
                for case in build_price_cases(corpus_text, args.with_price_cases * 3)
                if case["id"].removeprefix("price-") not in conflicted
            ][: args.with_price_cases]
            cases.extend(price_cases)

        generator: TextGenerator | None = (
            OllamaGenerator(model=args.model, timeout_seconds=180.0) if args.model else None
        )
        service = AnswerService(
            conn,
            generator=generator,
            hash_key="regression-hmac-key-0123456789",
            hash_key_id="k-reg",
            max_tokens=512,
            temperature=0.0,
        )

        rows: list[dict[str, Any]] = []
        passed = 0
        for case in cases:
            payload = _ask(
                service,
                query=case["query"],
                tenant=tenant,
                principal=principal,
                scope=case.get("scope", "internal_product"),
                trace_id=uuid.uuid4().hex,
            )
            conn.commit()
            ok, reasons = evaluate_case(payload, case.get("expect", {}))
            passed += int(ok)
            rows.append(
                {
                    "id": case["id"],
                    "passed": ok,
                    "reasons": reasons,
                    "status": payload.get("status"),
                    "answer": str(payload.get("answer", ""))[:120],
                }
            )

    summary = {"total": len(rows), "passed": passed, "pass_rate": round(passed / len(rows), 3) if rows else 0.0}
    print(f"回归结果：{passed}/{len(rows)} 通过（{summary['pass_rate']}）")
    for row in rows:
        mark = "PASS" if row["passed"] else "FAIL"
        print(f"  [{mark}] {row['id']}: {row['status']} {row['reasons']}")
    if args.output:
        Path(args.output).write_text(
            json.dumps({"summary": summary, "rows": rows}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"逐条结果写入 {args.output}")
    return 0 if passed == len(rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
