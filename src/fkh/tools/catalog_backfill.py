"""目录条目补录：对已入库的 `product_catalog` 版本重新抽取「货号 规格 单价」行。

为什么需要
----------
只在入库时抽取，救不了**已经入库**的历史文档（客户目录早就在库里了）。
补录必须可重复执行：同一货号同一原文行只计 duplicates，不重复写、不改原文、不动版本状态与 ACL。

用法
----
    uv run python -m fkh.tools.catalog_backfill \
        --url postgresql://... --currency CNY [--tenant <uuid>] [--dry-run]

`--currency` 必填：价格口径必须有明确来源，不提供默认值。
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from uuid import UUID

import psycopg

from fkh.workers.repository import IngestionRepository


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="目录条目补录（幂等）")
    parser.add_argument("--url", required=True, help="数据库连接串")
    parser.add_argument("--currency", required=True, choices=["CNY"], help="目录货币口径（必须显式）")
    parser.add_argument("--tenant", default=None, help="只处理指定租户（默认全部）")
    parser.add_argument("--dry-run", action="store_true", help="只统计不写库")
    args = parser.parse_args(argv)

    with psycopg.connect(args.url, autocommit=False) as conn:
        sql = """
            SELECT v.tenant_id, v.id
              FROM knowledge_versions v
              JOIN knowledge_documents d
                ON d.tenant_id = v.tenant_id AND d.id = v.document_id
             WHERE d.domain = 'product_catalog'
        """
        params: list[object] = []
        if args.tenant:
            sql += " AND v.tenant_id = %s"
            params.append(UUID(args.tenant))
        sql += " ORDER BY v.created_at"

        versions = conn.execute(sql, params).fetchall()
        print(f"待处理版本 {len(versions)} 个")

        totals = {"parsed": 0, "inserted": 0, "duplicates": 0, "conflicts": 0, "unsupported": 0}
        for tenant_id, version_id in versions:
            conn.execute(
                "SELECT set_config('app.tenant_id', %s, true)", (str(tenant_id),)
            )
            if args.dry_run:
                print(f"[dry-run] {version_id}")
                continue
            stats = IngestionRepository(conn, tenant_id).extract_catalog_items(
                version_id=version_id, currency=args.currency
            )
            for key in totals:
                totals[key] += getattr(stats, key)
            conn.commit()
            print(f"{version_id}: {stats.as_warning()}")

        print("汇总:", " ".join(f"{key}={value}" for key, value in totals.items()))
        if args.dry_run:
            conn.rollback()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
