"""迁移执行器：按序应用 SQL，记录校验和，支持回滚与漂移检测。

用法：
    uv run python migrations/apply.py --url "$FKH_DATABASE_URL"
    uv run python migrations/apply.py --url ... --status
    uv run python migrations/apply.py --url ... --rollback 1

设计约束：
- 每个迁移文件在独立事务内执行，失败即整体回滚，不留半成品 schema。
- 已应用迁移的内容一旦变化即视为漂移并拒绝继续，避免"改历史迁移"造成环境分叉。
- 回滚脚本必须与迁移文件同编号，缺失时显式失败，不静默跳过。
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
from pathlib import Path

import psycopg

MIGRATIONS_DIR = Path(__file__).resolve().parent
ROLLBACK_DIR = MIGRATIONS_DIR / "rollback"

# 依赖 pgvector 的迁移。实例缺少该扩展时跳过而非失败，
# 使治理层与 ACL 层可在无向量能力的环境（本地内嵌 PG、精简 CI）验证。
OPTIONAL_MIGRATIONS = {"0002"}

VERSION_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
  version    text PRIMARY KEY,
  checksum   text NOT NULL,
  applied_at timestamptz NOT NULL DEFAULT now()
);
"""


def discover_migrations() -> list[Path]:
    return sorted(MIGRATIONS_DIR.glob("[0-9][0-9][0-9][0-9]_*.sql"))


def file_checksum(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def version_of(path: Path) -> str:
    return path.name.split("_", 1)[0]


def fetch_applied(conn: psycopg.Connection) -> dict[str, str]:
    with conn.cursor() as cur:
        cur.execute(VERSION_TABLE_SQL)
        cur.execute("SELECT version, checksum FROM schema_migrations ORDER BY version")
        rows = cur.fetchall()
    conn.commit()
    return {str(version): str(checksum) for version, checksum in rows}


def assert_no_drift(files: list[Path], applied: dict[str, str]) -> None:
    problems: list[str] = []
    for path in files:
        version = version_of(path)
        if version in applied and applied[version] != file_checksum(path):
            problems.append(f"{path.name}: 已应用版本内容被修改（校验和漂移）")
    if problems:
        detail = "\n  - ".join(problems)
        raise SystemExit(f"检测到迁移漂移，拒绝执行：\n  - {detail}\n请改用新增迁移文件表达变更。")


def vector_available(conn: psycopg.Connection) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_available_extensions WHERE name = 'vector'")
        return cur.fetchone() is not None


def apply_all(
    conn: psycopg.Connection,
    files: list[Path],
    applied: dict[str, str],
    *,
    dry_run: bool,
) -> int:
    pending = [p for p in files if version_of(p) not in applied]
    if not pending:
        print("没有待应用迁移。")
        return 0

    has_vector = vector_available(conn)
    executed = 0
    skipped: list[str] = []

    for path in pending:
        version = version_of(path)
        if version in OPTIONAL_MIGRATIONS and not has_vector:
            print(f"~~ {path.name} 跳过：实例未提供 pgvector 扩展")
            skipped.append(path.name)
            continue
        print(f"-> {path.name}")
        if dry_run:
            continue
        with conn.transaction():
            conn.execute(path.read_text(encoding="utf-8"))
            conn.execute(
                "INSERT INTO schema_migrations (version, checksum) VALUES (%s, %s)",
                (version, file_checksum(path)),
            )
        executed += 1

    print(f"已应用 {executed} 个迁移。")
    if skipped:
        print(f"已跳过 {len(skipped)} 个可选迁移（未记入 schema_migrations）：{', '.join(skipped)}")
        print("在含 pgvector 的环境重新执行即可补齐；跳过不等于通过。")
    return executed


def rollback_last(conn: psycopg.Connection, steps: int) -> None:
    applied = fetch_applied(conn)
    if not applied:
        print("没有可回滚的迁移。")
        return

    targets = sorted(applied, reverse=True)[:steps]
    for version in targets:
        script = ROLLBACK_DIR / f"{version}_down.sql"
        if not script.exists():
            raise SystemExit(f"缺少回滚脚本：{script.name}，已中止，未做任何变更。")
        print(f"<- {script.name}")
        with conn.transaction():
            conn.execute(script.read_text(encoding="utf-8"))
            conn.execute("DELETE FROM schema_migrations WHERE version = %s", (version,))
    print(f"已回滚 {len(targets)} 个迁移。")


def print_status(files: list[Path], applied: dict[str, str]) -> None:
    print(f"{'版本':<8}{'状态':<10}{'文件'}")
    for path in files:
        version = version_of(path)
        state = "APPLIED" if version in applied else "PENDING"
        print(f"{version:<8}{state:<10}{path.name}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Factory Knowledge Hub 迁移执行器")
    parser.add_argument("--url", default=os.environ.get("FKH_DATABASE_URL"), help="PostgreSQL 连接串")
    parser.add_argument("--status", action="store_true", help="仅显示迁移状态")
    parser.add_argument("--rollback", type=int, metavar="N", help="回滚最近 N 个迁移")
    parser.add_argument("--dry-run", action="store_true", help="只打印待执行文件，不写库")
    args = parser.parse_args(argv)

    if not args.url:
        parser.error("未提供数据库连接串：使用 --url 或设置 FKH_DATABASE_URL")

    files = discover_migrations()
    if not files:
        raise SystemExit(f"{MIGRATIONS_DIR} 下未找到迁移文件。")

    with psycopg.connect(args.url, autocommit=False) as conn:
        applied = fetch_applied(conn)
        if args.status:
            print_status(files, applied)
            return 0
        if args.rollback:
            rollback_last(conn, args.rollback)
            return 0
        assert_no_drift(files, applied)
        apply_all(conn, files, applied, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
