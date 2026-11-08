"""随包内嵌 PostgreSQL 的启停与二进制路径（演示与交付共用）。

从 `demo_app` 抽出来，避免"客户交付入口"依赖演示模块。
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any


def pg_bin() -> Path:
    """复用随包安装的 PG 16 + pgvector 二进制。"""
    import pgserver

    return Path(pgserver.__file__).parent / "pginstall" / "bin"


def run_pg(args: list[str], *, capture: bool = True) -> subprocess.CompletedProcess[Any]:
    """跑 PG 二进制。

    `pg_ctl start` **不能**用管道抓输出：它启动的 postgres 子进程会继承 stdout，
    父进程读不到 EOF 就一直等下去（实测卡死 4 分钟）。启动这类命令一律用 DEVNULL。
    """
    if not capture:
        return subprocess.run(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace")


def ensure_postgres(pgdata: Path, port: int) -> str:
    """确保内嵌 PG 在跑，返回 DSN（postgres 超级用户，仅供迁移与管理）。

    不用 pgserver.get_server：它内部 `pg_ctl -w` 只等 10 秒，而 HDD 上非正常关机后的
    fsync 恢复可能 30 秒以上，会被误判成启动失败（实测踩过两次）。
    """
    pgdata.mkdir(parents=True, exist_ok=True)
    bin_dir = pg_bin()

    if not (pgdata / "PG_VERSION").exists():
        created = run_pg(
            [
                str(bin_dir / "initdb.exe"),
                "-D", str(pgdata),
                "-U", "postgres",
                "-A", "trust",
                "--encoding=UTF8",
                "--locale=C",
            ]
        )
        if created.returncode != 0:
            raise SystemExit(f"initdb 失败：\n{created.stdout}\n{created.stderr}")

    status = run_pg([str(bin_dir / "pg_ctl.exe"), "-D", str(pgdata), "status"])
    if status.returncode != 0:
        started = run_pg(
            [
                str(bin_dir / "pg_ctl.exe"),
                "-D", str(pgdata),
                "-o", f"-p {port} -h 127.0.0.1",
                "-t", "180",
                "-l", str(pgdata / "log"),
                "start",
            ],
            capture=False,
        )
        if started.returncode != 0:
            log = (pgdata / "log").read_text(encoding="utf-8", errors="replace")[-2000:]
            raise SystemExit(f"PostgreSQL 启动失败：\n{started.stdout}\n{started.stderr}\n{log}")
    return f"postgresql://postgres:@127.0.0.1:{port}/postgres"


def stop_postgres(pgdata: Path) -> None:
    bin_dir = pg_bin()
    run_pg([str(bin_dir / "pg_ctl.exe"), "-D", str(pgdata), "-m", "fast", "-w", "-t", "120", "stop"])


__all__ = ["ensure_postgres", "pg_bin", "run_pg", "stop_postgres"]
