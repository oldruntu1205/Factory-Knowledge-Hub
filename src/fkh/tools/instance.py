"""客户实例生命周期（交付入口，与演示播种完全分离）。

一条命令完成"空环境首装"，可重复执行；不播种任何演示语料。

设计要点（ADR-009：一客户一套独立私域实例）：
- 数据目录、端口、数据库、密钥、租户全部由参数决定，互不复用；
- **迁移用管理员**（postgres），**API 用业务角色**（非 owner、无 SUPERUSER、无 BYPASSRLS）；
- JWT 密钥写在实例目录里，重启后不变（令牌跨重启仍有效）；
- 域与目录规则属于实例配置，首装时显式登记（不依赖运行时隐式补建）。

用法：
    python -m fkh.tools.instance provision --workdir .instance-customer-a \
        --db-port 55471 --api-port 8141 --db-name fkh_customer_a
    python -m fkh.tools.instance serve --workdir .instance-customer-a
    python -m fkh.tools.instance backup --workdir .instance-customer-a --out backup.dump
    python -m fkh.tools.instance restore --from backup.dump \
        --workdir .instance-restored --db-port 55472 --api-port 8142
    python -m fkh.tools.instance status --workdir .instance-customer-a
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import secrets
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import psycopg

from fkh.tools.pg import ensure_postgres, pg_bin, run_pg, stop_postgres

DEFAULT_DOMAINS = (
    ("product_catalog", "产品目录"),
    ("quality", "质量与检验"),
    ("equipment_safety", "设备与安全"),
    ("training", "培训材料"),
)
DEFAULT_CATALOG_PATTERN = r"^[A-Z]{2}-[0-9]{4}$"
DEFAULT_CURRENCIES = ["CNY"]


# ---------------------------------------------------------------- 实例元数据
def instance_path(workdir: Path) -> Path:
    return workdir / "instance.json"


def load_instance(workdir: Path) -> dict[str, Any]:
    path = instance_path(workdir)
    if not path.exists():
        raise SystemExit(f"实例未初始化：{path} 不存在（先跑 provision）")
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


def _write_instance(workdir: Path, data: dict[str, Any]) -> None:
    path = instance_path(workdir)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _secret_path(workdir: Path) -> Path:
    return workdir / "secrets" / "jwt.secret"


def _ensure_secret(workdir: Path, *, force_new: bool = False) -> str:
    path = _secret_path(workdir)
    if path.exists() and not force_new:
        return path.read_text(encoding="utf-8").strip()
    path.parent.mkdir(parents=True, exist_ok=True)
    value = secrets.token_urlsafe(48)
    path.write_text(value + "\n", encoding="utf-8")
    with contextlib.suppress(OSError):  # Windows 上 chmod 语义有限，失败不影响功能
        os.chmod(path, 0o600)
    return value


# ---------------------------------------------------------------- 建库与角色
def _conn_args(dsn: str) -> tuple[list[str], str]:
    """把 DSN 拆成 pg_dump/pg_restore 认的显式参数（Windows 下直接传 DSN 会被当成多余参数）。"""
    parsed = urlparse(dsn)
    args = [
        "-h", str(parsed.hostname or "127.0.0.1"),
        "-p", str(parsed.port or 5432),
        "-U", str(parsed.username or "postgres"),
    ]
    return args, (parsed.path or "/postgres").lstrip("/")


def _exec(admin_dsn: str, sql: str) -> None:
    """用 psycopg 执行 DDL（psql 的 -v/-c 在 Windows 上易被吞参数，实测踩过）。"""
    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        conn.execute(sql)


def _database_exists(admin_dsn: str, db_name: str) -> bool:
    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        row = conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (db_name,)).fetchone()
    return row is not None


def _create_database(admin_dsn: str, db_name: str) -> None:
    if _database_exists(admin_dsn, db_name):
        return
    _exec(admin_dsn, f'CREATE DATABASE "{db_name}"')


def _role_name(db_name: str) -> str:
    suffix = db_name.replace("fkh_", "")[:16]
    return f"fkh_app_{suffix}"


def _ensure_business_role(admin_dsn: str, role: str, db_name: str) -> None:
    """业务角色：LOGIN，非 owner、非 SUPERUSER、无 BYPASSRLS；只给 DML/执行权限。"""
    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        exists = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone()
        if exists is None:
            conn.execute(f'CREATE ROLE "{role}" LOGIN')
        flags = conn.execute(
            "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = %s", (role,)
        ).fetchone()
        assert flags is not None
        if flags[0] or flags[1]:
            raise SystemExit(f"角色 {role} 具有 SUPERUSER/BYPASSRLS，拒绝作为业务角色")

    db_dsn = admin_dsn.rsplit("/", 1)[0] + "/" + db_name
    grants = [
        f'GRANT CONNECT ON DATABASE "{db_name}" TO "{role}"',
        f'GRANT USAGE ON SCHEMA public TO "{role}"',
        f'GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO "{role}"',
        f'GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO "{role}"',
        f'GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA public TO "{role}"',
        f'ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public '
        f'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO "{role}"',
        f'ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public '
        f'GRANT USAGE, SELECT ON SEQUENCES TO "{role}"',
    ]
    for sql in grants:
        _exec(db_dsn, sql)


def _apply_migrations(admin_dsn: str) -> None:
    from migrations.apply import main as apply_main

    code = apply_main(["--url", admin_dsn])
    if code not in (0, None):
        raise SystemExit(f"迁移失败，退出码 {code}")


# ---------------------------------------------------------------- 首装
def want_port_free(port: int) -> None:
    with socket.socket() as sock:
        sock.settimeout(0.5)
        if sock.connect_ex(("127.0.0.1", port)) == 0:
            raise SystemExit(f"端口 {port} 已被占用，请换一个（实例之间互不复用）")


def command_provision(args: argparse.Namespace) -> int:
    workdir = Path(args.workdir).resolve()
    # 复跑时先停掉上一次的实例（否则 pgdata 被占用，Windows 直接 PermissionError）
    if (workdir / "instance.json").exists():
        with contextlib.suppress(Exception):
            command_stop(argparse.Namespace(workdir=str(workdir)))
        time.sleep(2)
    workdir.mkdir(parents=True, exist_ok=True)
    pgdata = (workdir / "pgdata").resolve()

    want_port_free(args.api_port)
    admin_dsn_root = ensure_postgres(pgdata, args.db_port)
    _create_database(admin_dsn_root, args.db_name)
    db_dsn = admin_dsn_root.rsplit("/", 1)[0] + "/" + args.db_name

    _apply_migrations(db_dsn)  # 管理员执行迁移

    role = _role_name(args.db_name)
    _ensure_business_role(db_dsn, role, args.db_name)
    app_dsn = db_dsn.replace("postgres:@", f"{role}@")
    secret = _ensure_secret(workdir)

    record: dict[str, Any] = {
        "name": args.name,
        "workdir": str(workdir),
        "pgdata": str(pgdata),
        "db_port": args.db_port,
        "db_name": args.db_name,
        "api_port": args.api_port,
        "admin_dsn": db_dsn,
        "app_dsn": app_dsn,
        "app_role": role,
        "secret_file": str(_secret_path(workdir)),
        "created_at": datetime.now(UTC).isoformat(),
    }

    with psycopg.connect(db_dsn) as conn:
        tenant = uuid.uuid4()
        ops = uuid.uuid4()
        agent = uuid.uuid4()
        conn.execute("INSERT INTO tenants (id, name) VALUES (%s, %s)", (tenant, args.name))
        conn.execute(
            "INSERT INTO principals (tenant_id, id, external_subject, principal_type)"
            " VALUES (%s, %s, %s, 'user')",
            (tenant, ops, args.ops_subject),
        )
        conn.execute(
            "INSERT INTO principals (tenant_id, id, external_subject, principal_type)"
            " VALUES (%s, %s, %s, 'agent')",
            (tenant, agent, args.agent_subject),
        )
        conn.execute("SELECT set_config('app.tenant_id', %s, false)", (str(tenant),))
        for code, label in DEFAULT_DOMAINS:
            conn.execute(
                "INSERT INTO knowledge_domains (tenant_id, code, label, is_default)"
                " VALUES (%s, %s, %s, true) ON CONFLICT DO NOTHING",
                (tenant, code, label),
            )
        conn.execute(
            "INSERT INTO catalog_rules (tenant_id, code_pattern, currencies)"
            " VALUES (%s, %s, %s) ON CONFLICT (tenant_id) DO NOTHING",
            (tenant, DEFAULT_CATALOG_PATTERN, DEFAULT_CURRENCIES),
        )
        conn.commit()

    record.update(
        {
            "tenant_id": str(tenant),
            "ops_principal": str(ops),
            "agent_principal": str(agent),
            "jwt_secret": secret,  # 仅本地实例文件；备份/交付时脱敏
        }
    )
    _write_instance(workdir, record)
    print(json.dumps({k: v for k, v in record.items() if k != "jwt_secret"}, ensure_ascii=False))
    return 0


# ---------------------------------------------------------------- 启动 API
def command_serve(args: argparse.Namespace) -> int:
    workdir = Path(args.workdir).resolve()
    record = load_instance(workdir)
    # 实例重启必须把内嵌 PG 一起拉起来（stop 会连库一起停）
    ensure_postgres(Path(record["pgdata"]), int(record["db_port"]))
    secret = _ensure_secret(workdir)
    env = dict(os.environ)
    env.update(
        {
            "FKH_JWT_SECRET": secret,
            "FKH_DATABASE_URL": record["app_dsn"],
            "FKH_RETRIEVAL_HASH_KEY": secret[:32],
            "FKH_RETRIEVAL_HASH_KEY_ID": f"k-{record['name'][:8]}",
            "FKH_LOG_LEVEL": env.get("FKH_LOG_LEVEL", "info"),
        }
    )
    cmd = [
        sys.executable, "-m", "uvicorn", "fkh.api.main:app",
        "--host", "127.0.0.1", "--port", str(record["api_port"]),
    ]
    log = workdir / "api.log"
    with log.open("a", encoding="utf-8") as handle:
        proc = subprocess.Popen(cmd, env=env, stdout=handle, stderr=subprocess.STDOUT)
    (workdir / "api.pid").write_text(str(proc.pid), encoding="utf-8")

    deadline = time.time() + float(args.wait_seconds)
    url = f"http://127.0.0.1:{record['api_port']}/health/ready"
    while time.time() < deadline:
        time.sleep(1)
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                if resp.status == 200:
                    print(json.dumps({"api": f"http://127.0.0.1:{record['api_port']}",
                                      "pid": proc.pid, "ready": True}))
                    return 0
        except (urllib.error.URLError, TimeoutError):
            continue
    raise SystemExit(f"API 未就绪，日志：{log}")


def command_stop(args: argparse.Namespace) -> int:
    workdir = Path(args.workdir).resolve()
    pid_file = workdir / "api.pid"
    if pid_file.exists():
        pid = int(pid_file.read_text(encoding="utf-8").strip() or 0)
        if pid:
            subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True)
        pid_file.unlink(missing_ok=True)
    record = load_instance(workdir)
    stop_postgres(Path(record["pgdata"]))
    print(json.dumps({"stopped": True}))
    return 0


# ---------------------------------------------------------------- 备份 / 恢复
def command_backup(args: argparse.Namespace) -> int:
    workdir = Path(args.workdir).resolve()
    record = load_instance(workdir)
    out = Path(args.out).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    conn_args, db_name = _conn_args(record["admin_dsn"])
    result = run_pg(
        [
            str(pg_bin() / "pg_dump.exe"),
            *conn_args,
            "--format=custom",
            "--no-owner",
            "--no-privileges",
            "-f", str(out),
            db_name,
        ]
    )
    if result.returncode != 0:
        raise SystemExit(f"备份失败：\n{result.stdout}\n{result.stderr}")
    print(json.dumps({"backup": str(out), "bytes": out.stat().st_size}))
    return 0


def command_restore(args: argparse.Namespace) -> int:
    workdir = Path(args.workdir).resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    pgdata = (workdir / "pgdata").resolve()
    dump = Path(args.from_dump).resolve()
    if not dump.exists():
        raise SystemExit(f"备份文件不存在：{dump}")

    want_port_free(args.api_port)
    admin_dsn_root = ensure_postgres(pgdata, args.db_port)
    _create_database(admin_dsn_root, args.db_name)
    db_dsn = admin_dsn_root.rsplit("/", 1)[0] + "/" + args.db_name

    role = _role_name(args.db_name)
    with psycopg.connect(admin_dsn_root, autocommit=True) as conn:
        if conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone() is None:
            conn.execute(f'CREATE ROLE "{role}" LOGIN')
    _ensure_business_role(db_dsn, role, args.db_name)

    conn_args, db_name = _conn_args(db_dsn)
    result = run_pg(
        [
            str(pg_bin() / "pg_restore.exe"),
            *conn_args,
            "--no-owner", "--no-privileges",
            "-d", db_name, str(dump),
        ]
    )
    if result.returncode != 0:
        raise SystemExit(f"恢复失败：\n{result.stdout}\n{result.stderr}")

    # 恢复后的实例用**独立密钥**（与源实例令牌互不通用）
    secret = _ensure_secret(workdir, force_new=True)
    with psycopg.connect(db_dsn) as conn:
        row = conn.execute("SELECT id, name FROM tenants ORDER BY created_at LIMIT 1").fetchone()
        assert row is not None
        tenant = str(row[0])
        principals = conn.execute(
            "SELECT id, principal_type FROM principals WHERE tenant_id = %s", (tenant,)
        ).fetchall()
    ops = next((str(p[0]) for p in principals if str(p[1]) == "user"), None)
    agent = next((str(p[0]) for p in principals if str(p[1]) == "agent"), None)

    record = {
        "name": args.name,
        "workdir": str(workdir),
        "pgdata": str(pgdata),
        "db_port": args.db_port,
        "db_name": args.db_name,
        "api_port": args.api_port,
        "admin_dsn": db_dsn,
        "app_dsn": db_dsn.replace("postgres:@", f"{role}@"),
        "app_role": role,
        "secret_file": str(_secret_path(workdir)),
        "created_at": datetime.now(UTC).isoformat(),
        "restored_from": str(dump),
        "tenant_id": tenant,
        "ops_principal": ops,
        "agent_principal": agent,
        "jwt_secret": secret,
    }
    _write_instance(workdir, record)
    print(json.dumps({k: v for k, v in record.items() if k != "jwt_secret"}, ensure_ascii=False))
    return 0


# ---------------------------------------------------------------- 状态
def command_status(args: argparse.Namespace) -> int:
    record = load_instance(Path(args.workdir).resolve())
    with psycopg.connect(record["admin_dsn"]) as conn:
        conn.execute("SELECT set_config('app.tenant_id', %s, false)", (record["tenant_id"],))
        counts: dict[str, int] = {}
        for table in (
            "knowledge_documents", "knowledge_versions", "knowledge_chunks",
            "catalog_items", "rag_queries", "evidence_access_log",
        ):
            row = conn.execute(
                f"SELECT count(*) FROM {table} WHERE tenant_id = %s", (record["tenant_id"],)
            ).fetchone()
            counts[table] = int(row[0]) if row is not None else 0
        versions = conn.execute(
            "SELECT status, count(*) FROM knowledge_versions WHERE tenant_id = %s"
            " GROUP BY status ORDER BY status",
            (record["tenant_id"],),
        ).fetchall()
    print(
        json.dumps(
            {
                "name": record["name"],
                "api_port": record["api_port"],
                "db_port": record["db_port"],
                "tenant_id": record["tenant_id"],
                "counts": counts,
                "versions": {str(s): int(c) for s, c in versions},
            },
            ensure_ascii=False,
        )
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="客户实例生命周期（交付入口）")
    sub = parser.add_subparsers(dest="command", required=True)

    provision = sub.add_parser("provision", help="空环境首装（建库/迁移/角色/租户/配置/密钥）")
    provision.add_argument("--workdir", required=True)
    provision.add_argument("--db-port", type=int, required=True)
    provision.add_argument("--api-port", type=int, required=True)
    provision.add_argument("--db-name", required=True)
    provision.add_argument("--name", default="customer")
    provision.add_argument("--ops-subject", default="ops@customer.local")
    provision.add_argument("--agent-subject", default="agent@customer.local")
    provision.set_defaults(func=command_provision)

    serve = sub.add_parser("serve", help="用业务角色启动 API（前台子进程）")
    serve.add_argument("--workdir", required=True)
    serve.add_argument("--wait-seconds", default="90")
    serve.set_defaults(func=command_serve)

    stop = sub.add_parser("stop", help="停 API 与内嵌 PG")
    stop.add_argument("--workdir", required=True)
    stop.set_defaults(func=command_stop)

    backup = sub.add_parser("backup", help="pg_dump 备份（自定义格式，不含属主/授权）")
    backup.add_argument("--workdir", required=True)
    backup.add_argument("--out", required=True)
    backup.set_defaults(func=command_backup)

    restore = sub.add_parser("restore", help="恢复到全新隔离实例（新目录/新端口/新密钥）")
    restore.add_argument("--from", dest="from_dump", required=True)
    restore.add_argument("--workdir", required=True)
    restore.add_argument("--db-port", type=int, required=True)
    restore.add_argument("--api-port", type=int, required=True)
    restore.add_argument("--db-name", required=True)
    restore.add_argument("--name", default="restored")
    restore.set_defaults(func=command_restore)

    status = sub.add_parser("status", help="查看实例数据面（计数/版本状态）")
    status.add_argument("--workdir", required=True)
    status.set_defaults(func=command_status)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
