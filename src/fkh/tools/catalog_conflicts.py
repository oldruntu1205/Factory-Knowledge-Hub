"""目录冲突复核队列的命令行入口（运营侧）。

- `list`：列出某租户的待办组与候选（默认只列 OPEN）。必须显式 `--tenant`：
  跨租户汇总是单独授权的运营能力，不能因为"省略参数"就自动获得。
- `resolve`：KEEP 指定候选（`--keep <candidate_id>`）或 `--drop-all`。
  操作者必须是该租户的活跃 user 主体——`--actor` 只是声明，会拿去数据库核对。

决议不可原地修改（一次决议生效）；错判走新版本修正，不在本切片提供改判。
"""

from __future__ import annotations

import argparse
import csv
import io
from collections.abc import Sequence
from pathlib import Path
from uuid import UUID

import psycopg

from fkh.catalog.conflicts import (
    ConflictError,
    ConflictNotFound,
    list_open_groups,
    open_summary,
    require_operator,
    resolve_group,
)


def _cmd_list(args: argparse.Namespace) -> int:
    tenant = UUID(args.tenant)
    with psycopg.connect(args.url, autocommit=True) as conn:
        conn.execute("SELECT set_config('app.tenant_id', %s, false)", (str(tenant),))
        groups, candidates = open_summary(conn, tenant)
        print(f"tenant={tenant} open_groups={groups} open_candidates={candidates}")
        if args.status != "OPEN":
            print("（当前只支持列出 OPEN 待办）")
            return 0
        for group in list_open_groups(conn, tenant, limit=args.limit, offset=args.offset):
            print(
                f"\n[{group.group_id}] version={group.version_id} code={group.code} "
                f"status={group.status} candidates={len(group.candidates)}"
            )
            for candidate in group.candidates:
                print(
                    f"  - candidate={candidate.candidate_id} chunk={candidate.chunk_id} "
                    f"spec={candidate.spec} price={candidate.raw_price}"
                )
                print(f"    raw_text={candidate.raw_text}")
    return 0


def _cmd_export(args: argparse.Namespace) -> int:
    """把待办导出成运营可读的清单（md / csv），只读、不改数据。"""
    tenant = UUID(args.tenant)
    with psycopg.connect(args.url, autocommit=True) as conn:
        conn.execute("SELECT set_config('app.tenant_id', %s, false)", (str(tenant),))
        groups, candidates = open_summary(conn, tenant)
        items = list_open_groups(conn, tenant, limit=args.limit, offset=args.offset)

    if args.format == "csv":
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(["code", "version_id", "candidate_id", "chunk_id", "spec", "price", "raw_text"])
        for group in items:
            for candidate in group.candidates:
                writer.writerow(
                    [
                        group.code,
                        group.version_id,
                        candidate.candidate_id,
                        candidate.chunk_id,
                        candidate.spec,
                        candidate.raw_price,
                        candidate.raw_text,
                    ]
                )
        text = buffer.getvalue()
    else:
        lines = [
            f"# 目录冲突待办（tenant={tenant}）",
            "",
            f"- 待办组：**{groups}**",
            f"- 候选行：{candidates}",
            f"- 本页：{len(items)} 组（limit={args.limit}, offset={args.offset}）",
            "",
            "| 货号 | 版本 | 候选 | candidate_id | 规格 | 单价 | 原文行 |",
            "|---|---|---|---|---|---|---|",
        ]
        for group in items:
            for index, candidate in enumerate(group.candidates):
                first = index == 0
                lines.append(
                    "| {code} | {version} | {cand} | `{cid}` | {spec} | {price} | {raw} |".format(
                        code=group.code if first else "",
                        version=str(group.version_id)[:8] if first else "",
                        cand=first and str(len(group.candidates)) or "",
                        cid=candidate.candidate_id,
                        spec=candidate.spec.replace("|", "\\|"),
                        price=candidate.raw_price,
                        raw=candidate.raw_text.replace("|", "\\|"),
                    )
                )
        text = "\n".join(lines) + "\n"

    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"已导出 {len(items)} 组到 {args.out}")
    else:
        print(text)
    return 0


def _cmd_resolve(args: argparse.Namespace) -> int:
    tenant = UUID(args.tenant)
    version = UUID(args.version)
    actor = UUID(args.actor)
    keep = UUID(args.keep) if args.keep else None

    with psycopg.connect(args.url, autocommit=False) as conn:
        conn.execute("SELECT set_config('app.tenant_id', %s, true)", (str(tenant),))
        try:
            require_operator(conn, tenant, actor)
            group = resolve_group(
                conn,
                tenant,
                version_id=version,
                code=args.code,
                actor_id=actor,
                candidate_id=keep,
                drop_all=args.drop_all,
                note=args.note,
            )
            conn.commit()
        except (ConflictError, ConflictNotFound) as exc:
            conn.rollback()
            print(f"决议被拒绝：{exc}")
            return 2

    print(
        f"已决议：group={group.group_id} code={group.code} status={group.status} "
        f"selected={group.selected_candidate_id}"
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="目录冲突复核队列")
    parser.add_argument("--url", required=True, help="数据库连接串")
    sub = parser.add_subparsers(dest="command", required=True)

    listing = sub.add_parser("list", help="列出待办组")
    listing.add_argument("--tenant", required=True, help="租户 UUID（必须显式）")
    listing.add_argument("--status", default="OPEN", choices=["OPEN"])
    listing.add_argument("--limit", type=int, default=50)
    listing.add_argument("--offset", type=int, default=0)
    listing.set_defaults(func=_cmd_list)

    resolve = sub.add_parser("resolve", help="做决议（KEEP 或 DROP_ALL）")
    resolve.add_argument("--tenant", required=True)
    resolve.add_argument("--version", required=True)
    resolve.add_argument("--code", required=True)
    resolve.add_argument("--keep", default=None, help="选中的 candidate_id")
    resolve.add_argument("--drop-all", action="store_true", help="该货号不设条目")
    resolve.add_argument("--actor", required=True, help="操作者 principal UUID（必须是该租户活跃 user）")
    resolve.add_argument("--note", default=None)
    resolve.set_defaults(func=_cmd_resolve)

    export = sub.add_parser("export", help="导出待办清单（只读）")
    export.add_argument("--tenant", required=True)
    export.add_argument("--format", default="md", choices=["md", "csv"])
    export.add_argument("--out", default=None, help="输出文件；省略则打印到 stdout")
    export.add_argument("--limit", type=int, default=200)
    export.add_argument("--offset", type=int, default=0)
    export.set_defaults(func=_cmd_export)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
