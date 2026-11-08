"""随包内嵌 PostgreSQL 的启停与二进制路径（演示与交付共用）。

两种初始化模式：
- 默认 `initdb -A trust`：仅用于**本机演示**（数据目录不进交付）；
- `initial_superuser_password=...`：交付实例，`initdb` 用 scram-sha-256 + `--pwfile` 初始化，
  之后所有 host 连接都必须带密码（不再存在 trust 免密登录）。
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from fkh.tools import winacl


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


def init_cluster(pgdata: Path, *, superuser_password: str | None = None) -> None:
    """初始化集群。

    - `superuser_password` 为空：`-A trust`（演示用，仅本机）；
    - 非空：`--auth-host=scram-sha-256 --auth-local=trust --pwfile`（交付用）。
      密码文件写到系统临时目录，初始化后立即删除，不进入实例目录、不进日志。
    """
    pgdata.mkdir(parents=True, exist_ok=True)
    bin_dir = pg_bin()
    base = [
        str(bin_dir / "initdb.exe"),
        "-D", str(pgdata),
        "-U", "postgres",
        "--encoding=UTF8",
        "--locale=C",
    ]
    if superuser_password is None:
        args = [*base, "-A", "trust"]
        created = run_pg(args)
        if created.returncode != 0:
            raise SystemExit(f"initdb 失败：\n{created.stdout}\n{created.stderr}")
        return

    # 临时口令文件不能落在任何人都能读的 %TEMP% 根目录：先建私有目录并收紧 ACL，
    # 再在其中创建口令文件（文件继承目录 DACL，写入前就已私有）。
    private_dir = Path(tempfile.mkdtemp(prefix="fkh-pg-pw-"))
    try:
        winacl.tighten(private_dir, directory=True)
        pwfile = private_dir / "superuser.pw"
        pwfile.write_text(superuser_password + "\n", encoding="utf-8")
        winacl.tighten(pwfile, directory=False)
        created = run_pg([
            *base,
            "--auth-host=scram-sha-256",
            "--auth-local=trust",
            f"--pwfile={pwfile}",
        ])
    finally:
        shutil.rmtree(private_dir, ignore_errors=True)
    if created.returncode != 0:
        raise SystemExit(f"initdb（scram）失败：\n{created.stdout}\n{created.stderr}")


def ensure_postgres(
    pgdata: Path, port: int, *, superuser_password: str | None = None
) -> str:
    """确保内嵌 PG 在跑，返回 DSN。

    交付实例会带 `superuser_password`：首次初始化用 scram，之后连接串带密码。
    演示实例不传密码，保持 trust（本机演示，数据目录不入交付）。

    不用 pgserver.get_server：它内部 `pg_ctl -w` 只等 10 秒，而 HDD 上非正常关机后的
    fsync 恢复可能 30 秒以上，会被误判成启动失败（实测踩过两次）。
    """
    pgdata.mkdir(parents=True, exist_ok=True)
    bin_dir = pg_bin()

    if not (pgdata / "PG_VERSION").exists():
        init_cluster(pgdata, superuser_password=superuser_password)

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
    if superuser_password is None:
        return f"postgresql://postgres:@127.0.0.1:{port}/postgres"
    return f"postgresql://postgres:{superuser_password}@127.0.0.1:{port}/postgres"


def stop_postgres(pgdata: Path) -> None:
    bin_dir = pg_bin()
    run_pg([str(bin_dir / "pg_ctl.exe"), "-D", str(pgdata), "-m", "fast", "-w", "-t", "120", "stop"])


def postgres_status(pgdata: Path) -> bool:
    """集群是否在跑（用 pg_ctl status，不依赖端口）。"""
    result = run_pg([str(pg_bin() / "pg_ctl.exe"), "-D", str(pgdata), "status"])
    return result.returncode == 0


__all__ = [
    "ensure_postgres",
    "init_cluster",
    "pg_bin",
    "postgres_status",
    "run_pg",
    "stop_postgres",
]
