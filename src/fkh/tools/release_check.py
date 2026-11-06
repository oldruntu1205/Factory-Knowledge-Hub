"""上线前自检（只读，不写库、不调模型）。

产品线收口用：把"能不能上"变成可执行的检查，而不是靠文档里的自我评价。

检查项（任一 FAIL 退出码为 1）：
1. 数据库可达；
2. 迁移已应用到最新（按 `migrations/NNNN_*.sql` 与 `schema_migrations` 比对）；
3. 关键业务表启用了 FORCE ROW LEVEL SECURITY；
4. 语料/目录数据现状（租户、切片、目录条目、冲突待办）；
5. 生成能力现状（`FKH_LLM_PROVIDER`；ollama 时探测本机端口，不做模型调用）；
6. 审计表可读（证明连接账号有权限看到审计，不是只写了就完事）。
"""

from __future__ import annotations

import argparse
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg

TENANT_TABLES = (
    "knowledge_documents",
    "knowledge_versions",
    "knowledge_chunks",
    "catalog_items",
    "catalog_conflict_groups",
    "catalog_conflict_candidates",
    "rag_queries",
    "retrieval_queries",
)


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    status: str  # PASS / WARN / FAIL
    detail: str


def _latest_migration(directory: Path) -> str | None:
    names = sorted(path.name for path in directory.glob("[0-9][0-9][0-9][0-9]_*.sql"))
    return None if not names else names[-1][:4]


def _applied(conn: psycopg.Connection[Any]) -> set[str]:
    rows = conn.execute("SELECT version FROM schema_migrations").fetchall()
    return {str(row[0]) for row in rows}


def _rls_state(conn: psycopg.Connection[Any], table: str) -> tuple[bool, bool] | None:
    row = conn.execute(
        "SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = %s",
        (table,),
    ).fetchone()
    return None if row is None else (bool(row[0]), bool(row[1]))


def _ollama_reachable(host: str = "127.0.0.1", port: int = 11434, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def render(checks: list[Check]) -> str:
    width = max(len(check.name) for check in checks) if checks else 10
    lines = [f"{'检查项'.ljust(width)}  状态   说明"]
    for check in checks:
        lines.append(f"{check.name.ljust(width)}  {check.status:5s}  {check.detail}")
    failures = sum(1 for check in checks if check.status == "FAIL")
    warnings = sum(1 for check in checks if check.status == "WARN")
    lines.append("")
    lines.append(f"结论：{len(checks) - failures - warnings} PASS / {warnings} WARN / {failures} FAIL")
    lines.append("GO" if failures == 0 else "NO-GO")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="上线前自检（只读）")
    parser.add_argument("--url", required=True, help="数据库 DSN")
    parser.add_argument("--migrations", default="migrations")
    parser.add_argument("--provider", default="none", help="FKH_LLM_PROVIDER 当前值")
    args = parser.parse_args(argv)

    checks: list[Check] = []
    migrations_dir = Path(args.migrations)
    expected = _latest_migration(migrations_dir)

    try:
        conn = psycopg.connect(args.url)
    except Exception as exc:  # noqa: BLE001 - 自检要报告而不是抛栈
        checks.append(Check("数据库可达", "FAIL", f"{type(exc).__name__}: {exc}"))
        print(render(checks))
        return 1

    with conn:
        checks.append(Check("数据库可达", "PASS", "连接成功"))

        applied = _applied(conn) if _has_table(conn, "schema_migrations") else set()
        missing = [
            path.name
            for path in sorted(migrations_dir.glob("[0-9][0-9][0-9][0-9]_*.sql"))
            if path.name[:4] not in applied
        ]
        if missing:
            checks.append(
                Check("迁移已应用", "FAIL", f"缺少 {len(missing)} 个：{', '.join(missing[:3])}")
            )
        else:
            checks.append(Check("迁移已应用", "PASS", f"最新 {expected}"))

        for table in TENANT_TABLES:
            state = _rls_state(conn, table)
            if state is None:
                checks.append(Check(f"RLS {table}", "WARN", "表不存在"))
            elif not (state[0] and state[1]):
                checks.append(Check(f"RLS {table}", "FAIL", f"enable={state[0]} force={state[1]}"))
            else:
                checks.append(Check(f"RLS {table}", "PASS", "ENABLE + FORCE"))

        counts = _counts(conn)
        checks.append(
            Check(
                "语料与目录",
                "PASS" if counts["chunks"] > 0 else "WARN",
                f"租户 {counts['tenants']} / 切片 {counts['chunks']} / "
                f"目录条目 {counts['items']} / 冲突待办 {counts['conflicts']}",
            )
        )
        audit_row = conn.execute("SELECT count(*) FROM rag_queries").fetchone()
        audit = 0 if audit_row is None else int(audit_row[0])
        checks.append(Check("审计可读", "PASS", f"rag_queries {audit} 行"))

    if args.provider == "none":
        checks.append(Check("生成能力", "WARN", "FKH_LLM_PROVIDER=none：描述类问答返回 503，货号/报价不受影响"))
    elif args.provider == "ollama":
        reachable = _ollama_reachable()
        checks.append(
            Check(
                "生成能力",
                "PASS" if reachable else "FAIL",
                "ollama 端口可达" if reachable else "ollama 127.0.0.1:11434 不可达",
            )
        )
    else:
        checks.append(Check("生成能力", "FAIL", f"未知 provider：{args.provider}"))

    print(render(checks))
    return 0 if all(check.status != "FAIL" for check in checks) else 1


def _has_table(conn: psycopg.Connection[Any], table: str) -> bool:
    row = conn.execute("SELECT to_regclass(%s)", (f"public.{table}",)).fetchone()
    return row is not None and row[0] is not None


def _counts(conn: psycopg.Connection[Any]) -> dict[str, int]:
    def scalar(sql: str) -> int:
        row = conn.execute(sql).fetchone()
        return 0 if row is None else int(row[0])

    return {
        "tenants": scalar("SELECT count(*) FROM tenants"),
        "chunks": scalar("SELECT count(*) FROM knowledge_chunks"),
        "items": scalar("SELECT count(*) FROM catalog_items"),
        "conflicts": scalar(
            "SELECT count(*) FROM catalog_conflict_groups WHERE status = 'OPEN'"
        ),
    }


if __name__ == "__main__":
    raise SystemExit(main())
