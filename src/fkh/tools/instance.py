"""客户实例生命周期（交付入口，与演示播种完全分离）。

安全基线（2026-11-08 客户交付加固）：
- 交付实例**不用 trust**：`initdb` 以 `--auth-host=scram-sha-256` + 临时 pwfile 初始化，
  管理员与 API 业务角色各自独立口令；
- 口令与 JWT 密钥只存在于 `<workdir>/secrets/`（0600），**不写进 instance.json**、
  不进命令行参数、不进日志；
- API 用业务角色（非 owner、无 SUPERUSER、无 BYPASSRLS）；
- 启动会校验既有密钥（长度/非占位），并确认"本次启动的子进程"真正就绪且是**本实例**；
- 已初始化实例再次 provision 默认拒绝（`--reuse` 幂等复用，保留原租户/主体/数据/令牌）；
- restore 只接受**全新隔离目标**，单事务恢复，失败即清理，不留半恢复的可运行状态。

用法：
    python -m fkh.tools.instance provision --workdir .instance-customer-a \
        --db-port 55501 --api-port 8151 --db-name fkh_customer_a
    python -m fkh.tools.instance serve --workdir .instance-customer-a
    python -m fkh.tools.instance backup --workdir .instance-customer-a --out a.dump
    python -m fkh.tools.instance restore --from a.dump --workdir .instance-customer-b \
        --db-port 55502 --api-port 8152 --db-name fkh_customer_b
    python -m fkh.tools.instance stop --workdir .instance-customer-a
    python -m fkh.tools.instance status --workdir .instance-customer-a
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse, urlunparse

import psycopg

from fkh.tools.pg import ensure_postgres, pg_bin, postgres_status, run_pg, stop_postgres

SCHEMA_VERSION = 1
MIN_SECRET_LEN = 32
DEFAULT_DOMAINS = (
    ("product_catalog", "产品目录"),
    ("quality", "质量与检验"),
    ("equipment_safety", "设备与安全"),
    ("training", "培训材料"),
)
DEFAULT_CATALOG_PATTERN = r"^[A-Z]{2}-[0-9]{4}$"
DEFAULT_CURRENCIES = ["CNY"]

SECRET_FILES = {"jwt": "jwt.secret", "db_admin": "db-admin.password", "db_app": "db-app.password"}


# ---------------------------------------------------------------- 密钥与连接串
def instance_path(workdir: Path) -> Path:
    return workdir / "instance.json"


def secret_path(workdir: Path, kind: str) -> Path:
    return workdir / "secrets" / SECRET_FILES[kind]


def load_instance(workdir: Path) -> dict[str, Any]:
    path = instance_path(workdir)
    if not path.exists():
        raise SystemExit(f"实例未初始化：{path} 不存在（先跑 provision）")
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


def _write_instance(workdir: Path, data: dict[str, Any]) -> None:
    path = instance_path(workdir)
    payload = json.dumps(data, ensure_ascii=False, indent=2)
    hidden = {"jwt_secret", "db_password", "admin_password", "app_password"}
    assert not any(f'"{name}"' in payload for name in hidden), "instance.json 不得包含密钥"
    path.write_text(payload, encoding="utf-8")


def _write_secret(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value + "\n", encoding="utf-8")
    with contextlib.suppress(OSError):
        os.chmod(path, 0o600)


def _read_secret(workdir: Path, kind: str, *, validate: bool = True) -> str:
    path = secret_path(workdir, kind)
    if not path.exists():
        raise SystemExit(f"缺少实例密钥文件：{path}（实例损坏或被手工删除，拒绝启动）")
    value = path.read_text(encoding="utf-8").strip()
    if validate:
        if len(value) < MIN_SECRET_LEN:
            raise SystemExit(f"实例密钥强度不足（<{MIN_SECRET_LEN} 字符）：{path}，拒绝启动")
        if set(value) <= {"x", "0", "a"}:
            raise SystemExit(f"实例密钥疑似占位值：{path}，拒绝启动")
    return value


def _new_secret() -> str:
    return secrets.token_urlsafe(36)


def _dsn(record: dict[str, Any], *, kind: str = "admin", db_name: str | None = None) -> str:
    """运行期构造 DSN（口令从密钥文件读），避免把口令写进配置或参数。"""
    workdir = Path(record["workdir"])
    user = record["admin_user"] if kind == "admin" else record["app_role"]
    password = _read_secret(workdir, "db_admin" if kind == "admin" else "db_app")
    return urlunparse(
        (
            "postgresql",
            f"{quote(user, safe='')}:{quote(password, safe='')}@"
            f"127.0.0.1:{record['db_port']}",
            "/" + (db_name or record["db_name"]),
            "",
            "",
            "",
        )
    )


def _masked(value: str) -> str:
    parsed = urlparse(value)
    host = parsed.hostname or "?"
    return f"postgresql://{parsed.username}:***@{host}:{parsed.port}{parsed.path}"


# ---------------------------------------------------------------- 端口与进程
def port_owner(port: int) -> dict[str, Any] | None:
    """返回占用端口的进程信息；未占用返回 None。"""
    import psutil  # type: ignore[import-untyped]

    for conn in psutil.net_connections(kind="tcp"):
        if conn.status == psutil.CONN_LISTEN and conn.laddr and conn.laddr.port == port:
            pid = conn.pid or 0
            name = ""
            if pid:
                with contextlib.suppress(Exception):
                    name = psutil.Process(pid).name()
            return {"pid": pid, "name": name}
    return None


def _refuse_if_busy(port: int, *, what: str) -> None:
    owner = port_owner(port)
    if owner is None:
        # psutil 可能看不到其它会话的监听；再用一次 TCP 连接兜底（Windows 允许 SO_REUSEADDR 共存）
        probe = socket.socket()
        try:
            probe.settimeout(0.5)
            busy = probe.connect_ex(("127.0.0.1", port)) == 0
        finally:
            probe.close()
        if not busy:
            return
        raise SystemExit(f"{what} 端口 {port} 已被占用（探测到已有服务应答）；实例之间端口不复用")
    raise SystemExit(
        f"{what} 端口 {port} 已被 pid={owner['pid']}（{owner['name']}）占用；"
        "实例之间端口不复用，请换端口或先停掉占用者"
    )


def _pid_matches(pid: int, needles: tuple[str, ...]) -> tuple[bool, str]:
    import psutil

    with contextlib.suppress(Exception):
        proc = psutil.Process(pid)
        cmdline = " ".join(proc.cmdline()).replace("\\", "/")
        for needle in needles:
            if needle.replace("\\", "/") in cmdline:
                return True, cmdline
        return False, cmdline
    return False, "进程不存在或无权限读取"


# ---------------------------------------------------------------- 建库与角色
def _exec(dsn: str, sql: str) -> None:
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(sql)


def _database_exists(admin_dsn: str, db_name: str) -> bool:
    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        row = conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (db_name,)).fetchone()
    return row is not None


def _create_database(admin_dsn: str, db_name: str) -> None:
    if _database_exists(admin_dsn, db_name):
        raise SystemExit(f"数据库 {db_name} 已存在；restore/provision 只接受全新隔离目标")
    _exec(admin_dsn, f'CREATE DATABASE "{db_name}"')


def _role_name(db_name: str) -> str:
    return f"fkh_app_{db_name.replace('fkh_', '')[:16]}"


def _ensure_business_role(db_root_dsn: str, db_dsn: str, role: str, password: str) -> None:
    """业务角色：LOGIN + 独立口令，非 owner、非 SUPERUSER、无 BYPASSRLS。"""
    raise_if_exists = "SELECT 1 FROM pg_roles WHERE rolname = %s"
    with psycopg.connect(db_root_dsn, autocommit=True) as conn:
        existing = conn.execute(raise_if_exists, (role,)).fetchone()
    if existing is None:
        _exec(db_root_dsn, f'CREATE ROLE "{role}" LOGIN')
    _exec(db_root_dsn, f'ALTER ROLE "{role}" PASSWORD {_sql_literal(password)}')

    with psycopg.connect(db_root_dsn, autocommit=True) as conn:
        flags = conn.execute(
            "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = %s", (role,)
        ).fetchone()
    assert flags is not None
    if flags[0] or flags[1]:
        raise SystemExit(f"角色 {role} 具有 SUPERUSER/BYPASSRLS，拒绝作为业务角色")

    grants = [
        f'GRANT CONNECT ON DATABASE "{Path(urlparse(db_dsn).path).name.lstrip("/")}" TO "{role}"',
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


def _sql_literal(value: str) -> str:
    escaped = value.replace("'", "''")
    return f"'{escaped}'"


def _apply_migrations(admin_dsn: str) -> None:
    from migrations.apply import main as apply_main

    code = apply_main(["--url", admin_dsn])
    if code not in (0, None):
        raise SystemExit(f"迁移失败，退出码 {code}")


def _assert_auth_isolation(record: dict[str, Any]) -> dict[str, Any]:
    """认证隔离实证：无口令 / 交叉口令都不能以管理员登录。"""
    admin_pw = _read_secret(Path(record["workdir"]), "db_admin")
    app_pw = _read_secret(Path(record["workdir"]), "db_app")
    host = f"127.0.0.1:{record['db_port']}"
    db = record["db_name"]

    def try_login(user: str, password: str) -> bool:
        dsn = urlunparse(
            ("postgresql", f"{quote(user, safe='')}:{quote(password, safe='')}@{host}", f"/{db}", "", "", "")
        )
        try:
            with psycopg.connect(dsn, connect_timeout=5) as conn:
                conn.execute("SELECT 1")
            return True
        except psycopg.OperationalError:
            return False

    results = {
        "admin_with_admin_pw": try_login("postgres", admin_pw),
        "admin_without_pw": try_login("postgres", ""),
        "admin_with_app_pw": try_login("postgres", app_pw),
        "app_with_app_pw": try_login(record["app_role"], app_pw),
        "app_with_admin_pw": try_login(record["app_role"], admin_pw),
    }
    assert results["admin_with_admin_pw"], "管理员自己都登不上"
    assert results["app_with_app_pw"], "业务角色登不上"
    if results["admin_without_pw"] or results["admin_with_app_pw"] or results["app_with_admin_pw"]:
        raise SystemExit(f"认证隔离失败：{results}")
    return results


# ---------------------------------------------------------------- 首装
def command_provision(args: argparse.Namespace) -> int:
    workdir = Path(args.workdir).resolve()
    if instance_path(workdir).exists():
        if not args.reuse:
            raise SystemExit(
                f"实例已初始化：{instance_path(workdir)}；"
                "若确认要复用（保留租户/主体/数据/令牌）请加 --reuse"
            )
        existing = load_instance(workdir)
        ensure_postgres(Path(existing["pgdata"]), int(existing["db_port"]),
                        superuser_password=_read_secret(workdir, "db_admin"))
        for kind in ("db_admin", "db_app", "jwt"):
            _read_secret(workdir, kind)
        print(json.dumps({"reused": True, "workdir": str(workdir),
                          "tenant_id": existing["tenant_id"]}, ensure_ascii=False))
        return 0

    if (workdir / "pgdata").exists() and any((workdir / "pgdata").iterdir()):
        raise SystemExit(f"目标目录已有数据：{workdir / 'pgdata'}；provision 只接受空环境")
    _refuse_if_busy(args.api_port, what="API")
    _refuse_if_busy(args.db_port, what="DB")

    workdir.mkdir(parents=True, exist_ok=True)
    pgdata = (workdir / "pgdata").resolve()
    admin_pw, app_pw = _new_secret(), _new_secret()
    _write_secret(secret_path(workdir, "db_admin"), admin_pw)
    _write_secret(secret_path(workdir, "db_app"), app_pw)

    root_dsn = ensure_postgres(pgdata, args.db_port, superuser_password=admin_pw)
    _create_database(root_dsn, args.db_name)
    db_dsn = urlunparse(urlparse(root_dsn)._replace(path="/" + args.db_name))

    _apply_migrations(db_dsn)  # 管理员执行 DDL

    role = _role_name(args.db_name)
    _ensure_business_role(root_dsn, db_dsn, role, app_pw)

    record: dict[str, Any] = {
        "schema": SCHEMA_VERSION,
        "name": args.name,
        "workdir": str(workdir),
        "pgdata": str(pgdata),
        "db_port": args.db_port,
        "db_name": args.db_name,
        "admin_user": "postgres",
        "app_role": role,
        "api_port": args.api_port,
        "secret_dir": "secrets",
        "domains": [code for code, _ in DEFAULT_DOMAINS],
        "created_at": datetime.now(UTC).isoformat(),
    }
    _write_secret(secret_path(workdir, "jwt"), _new_secret())

    with psycopg.connect(db_dsn) as conn:
        tenant, ops, agent = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
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
        }
    )
    _assert_auth_isolation(record)
    _write_instance(workdir, record)
    print(json.dumps({**record, "admin_dsn": _masked(_dsn(record))}, ensure_ascii=False))
    return 0


# ---------------------------------------------------------------- 启动 / 停止
def _identity_probe(record: dict[str, Any], secret: str) -> bool:
    """用本实例密钥签发令牌并读取域配置，确认应答者是**本实例**（旧服务不算）。"""
    import jwt

    now = datetime.now(UTC)
    token = jwt.encode(
        {
            "iss": "fkh.local", "aud": "fkh-api", "sub": "probe@instance.local",
            "iat": now, "exp": now + timedelta(minutes=5),
            "tenant_id": record["tenant_id"], "principal_id": record["ops_principal"],
            "roles": ["staff"], "scope": ["knowledge.read"],
            "allowed_scopes": ["internal_product", "customer_public"],
        },
        secret,
        algorithm="HS256",
    )
    url = f"http://127.0.0.1:{record['api_port']}/v1/config/domains"
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(request, timeout=10) as resp:
            body = json.loads(resp.read().decode())
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return False
    codes = {item["code"] for item in body.get("domains", [])}
    return codes == set(record.get("domains", []))


def command_serve(args: argparse.Namespace) -> int:
    workdir = Path(args.workdir).resolve()
    record = load_instance(workdir)
    secret = _read_secret(workdir, "jwt")  # 校验既有密钥（缺失/过短/占位直接拒绝）
    _refuse_if_busy(int(record["api_port"]), what="API")

    ensure_postgres(
        Path(record["pgdata"]), int(record["db_port"]),
        superuser_password=_read_secret(workdir, "db_admin"),
    )

    env = dict(os.environ)
    env.update(
        {
            "FKH_JWT_SECRET": secret,
            "FKH_DATABASE_URL": _dsn(record, kind="app"),  # 口令只在环境变量里，不进 argv
            "FKH_RETRIEVAL_HASH_KEY": secret[:32],
            "FKH_RETRIEVAL_HASH_KEY_ID": f"k-{record['name'][:8]}",
            "FKH_LOG_LEVEL": env.get("FKH_LOG_LEVEL", "info"),
        }
    )
    log_path = workdir / "api.log"
    with log_path.open("a", encoding="utf-8") as handle:
        proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "fkh.api.main:app",
             "--host", "127.0.0.1", "--port", str(record["api_port"])],
            env=env, stdout=handle, stderr=subprocess.STDOUT, cwd=workdir,
        )

    deadline = time.time() + float(args.wait_seconds)
    ready = False
    while time.time() < deadline:
        if proc.poll() is not None:
            raise SystemExit(f"本次启动的 API 子进程已退出（code={proc.returncode}），日志：{log_path}")
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{record['api_port']}/health/ready", timeout=3
            ) as resp:
                dep_ok = resp.status == 200
        except (urllib.error.URLError, TimeoutError):
            dep_ok = False
        if dep_ok and _identity_probe(record, secret):
            ready = True
            break
        time.sleep(1)
    if not ready:
        proc.terminate()
        raise SystemExit(f"API 未就绪或应答者不是本实例（端口 {record['api_port']}），日志：{log_path}")

    owner = port_owner(int(record["api_port"]))
    if owner is not None and owner["pid"] != proc.pid:
        # psutil 可能残留已关闭 socket 的历史条目：只有"另一个**活着的、也跑本实例**的进程"才算冲突
        import contextlib as _ctx

        import psutil

        alive_other = False
        with _ctx.suppress(Exception):
            other = psutil.Process(owner["pid"])
            cmd = " ".join(other.cmdline()).replace("\\", "/")
            alive_other = other.is_running() and workdir.as_posix() in cmd
        if alive_other:
            proc.terminate()
            raise SystemExit(
                f"端口 {record['api_port']} 已被另一个本实例进程占用（pid={owner['pid']}），"
                "拒绝重复启动"
            )
    if proc.poll() is not None:
        raise SystemExit(f"本次启动的 API 子进程已退出（code={proc.returncode}），日志：{log_path}")

    (workdir / "api.pid").write_text(str(proc.pid), encoding="utf-8")
    print(json.dumps({"api": f"http://127.0.0.1:{record['api_port']}",
                      "pid": proc.pid, "ready": True, "instance": record["name"]}))
    return 0


def command_stop(args: argparse.Namespace) -> int:
    workdir = Path(args.workdir).resolve()
    record = load_instance(workdir)
    pid_file = workdir / "api.pid"
    stopped_api = False
    if pid_file.exists():
        pid = int(pid_file.read_text(encoding="utf-8").strip() or 0)
        if pid:
            matched, cmdline = _pid_matches(
                pid, ("uvicorn", str(record["api_port"]), workdir.as_posix())
            )
            if not matched:
                raise SystemExit(
                    f"pid={pid} 不是本实例的 API 进程（cmdline={cmdline[:160]}），拒绝停止"
                )
            import psutil

            proc = psutil.Process(pid)
            proc.terminate()
            with contextlib.suppress(Exception):
                proc.wait(timeout=20)
            if proc.is_running():
                proc.kill()
            stopped_api = True
        pid_file.unlink(missing_ok=True)
    if stopped_api:
        deadline = time.time() + 20
        while time.time() < deadline:
            if port_owner(int(record["api_port"])) is None:
                break
            time.sleep(0.5)
        if port_owner(int(record["api_port"])) is not None:
            raise SystemExit(f"API 端口 {record['api_port']} 仍在监听，停止未完成")

    stop_postgres(Path(record["pgdata"]))
    if postgres_status(Path(record["pgdata"])):
        raise SystemExit("内嵌 PostgreSQL 仍在运行，停止未完成")
    print(json.dumps({"stopped": True, "api_was_running": stopped_api}))
    return 0


# ---------------------------------------------------------------- 备份 / 恢复
def command_backup(args: argparse.Namespace) -> int:
    workdir = Path(args.workdir).resolve()
    record = load_instance(workdir)
    out = Path(args.out).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    conn_args, db_name = _conn_args(_dsn(record, kind="admin"))
    result = _run_pg_with_password(
        [str(pg_bin() / "pg_dump.exe"), *conn_args,
         "--format=custom", "--no-owner", "--no-privileges", "-f", str(out), db_name],
        _read_secret(workdir, "db_admin"),
    )
    if result.returncode != 0:
        raise SystemExit(f"备份失败：\n{result.stdout}\n{result.stderr}")
    listed = run_pg([str(pg_bin() / "pg_restore.exe"), "--list", str(out)])
    if listed.returncode != 0:
        out.unlink(missing_ok=True)
        raise SystemExit("备份自检失败（pg_restore --list 无法读取）")
    print(json.dumps({"backup": str(out), "bytes": out.stat().st_size,
                      "entries": len(listed.stdout.strip().splitlines())}))
    return 0


def _run_pg_with_password(
    args: list[str], password: str
) -> subprocess.CompletedProcess[str]:
    """跑 pg_dump/pg_restore 并注入 PGPASSWORD（口令不进 argv、不进日志）。"""
    env = dict(os.environ)
    env["PGPASSWORD"] = password
    return subprocess.run(
        args, capture_output=True, text=True, encoding="utf-8", errors="replace", env=env
    )


def _conn_args(dsn: str) -> tuple[list[str], str]:
    parsed = urlparse(dsn)
    args = [
        "-h", str(parsed.hostname or "127.0.0.1"),
        "-p", str(parsed.port or 5432),
        "-U", str(parsed.username or "postgres"),
    ]
    return args, (parsed.path or "/postgres").lstrip("/")


def command_restore(args: argparse.Namespace) -> int:
    workdir = Path(args.workdir).resolve()
    dump = Path(args.from_dump).resolve()
    if not dump.exists():
        raise SystemExit(f"备份文件不存在：{dump}")
    if instance_path(workdir).exists():
        raise SystemExit(f"目标已是一个实例（{instance_path(workdir)}），restore 只接受全新隔离目标")
    pgdata = (workdir / "pgdata").resolve()
    if pgdata.exists() and any(pgdata.iterdir()):
        raise SystemExit(f"目标数据目录非空：{pgdata}，restore 只接受全新隔离目标")
    _refuse_if_busy(args.api_port, what="API")
    _refuse_if_busy(args.db_port, what="DB")

    listed = run_pg([str(pg_bin() / "pg_restore.exe"), "--list", str(dump)])
    if listed.returncode != 0:
        raise SystemExit(f"备份文件不可读，拒绝恢复：\n{listed.stdout}\n{listed.stderr}")

    workdir.mkdir(parents=True, exist_ok=True)
    admin_pw, app_pw = _new_secret(), _new_secret()
    _write_secret(secret_path(workdir, "db_admin"), admin_pw)
    _write_secret(secret_path(workdir, "db_app"), app_pw)

    root_dsn = ensure_postgres(pgdata, args.db_port, superuser_password=admin_pw)
    db_dsn = urlunparse(urlparse(root_dsn)._replace(path="/" + args.db_name))
    role = _role_name(args.db_name)

    created_db = False
    try:
        _create_database(root_dsn, args.db_name)
        created_db = True
        _ensure_business_role(root_dsn, db_dsn, role, app_pw)
        conn_args, db_name = _conn_args(db_dsn)
        restored = _run_pg_with_password(
            [str(pg_bin() / "pg_restore.exe"), *conn_args,
             "--no-owner", "--no-privileges", "--single-transaction",
             "-d", db_name, str(dump)],
            _read_secret(workdir, "db_admin"),
        )
        if restored.returncode != 0:
            raise RuntimeError(f"pg_restore 失败：\n{restored.stdout}\n{restored.stderr}")

        secret = _new_secret()
        _write_secret(secret_path(workdir, "jwt"), secret)
        with psycopg.connect(db_dsn) as conn:
            row = conn.execute("SELECT id, name FROM tenants ORDER BY created_at LIMIT 1").fetchone()
            assert row is not None
            tenant = str(row[0])
            principals = conn.execute(
                "SELECT id, principal_type FROM principals WHERE tenant_id = %s", (tenant,)
            ).fetchall()
            domains = [
                str(r[0])
                for r in conn.execute(
                    "SELECT code FROM knowledge_domains WHERE tenant_id = %s ORDER BY code",
                    (tenant,),
                ).fetchall()
            ]
        record: dict[str, Any] = {
            "schema": SCHEMA_VERSION,
            "name": args.name,
            "workdir": str(workdir),
            "pgdata": str(pgdata),
            "db_port": args.db_port,
            "db_name": args.db_name,
            "admin_user": "postgres",
            "app_role": role,
            "api_port": args.api_port,
            "secret_dir": "secrets",
            "domains": domains,
            "created_at": datetime.now(UTC).isoformat(),
            "restored_from": str(dump),
            "tenant_id": tenant,
            "ops_principal": next((str(p[0]) for p in principals if str(p[1]) == "user"), None),
            "agent_principal": next((str(p[0]) for p in principals if str(p[1]) == "agent"), None),
        }
        _assert_auth_isolation(record)
        _write_instance(workdir, record)
    except BaseException:
        # 半恢复清理：删库、停库、清掉目标目录，保证不留下可运行的半成品
        with contextlib.suppress(Exception):
            if created_db:
                _exec(root_dsn, f'DROP DATABASE IF EXISTS "{args.db_name}" WITH (FORCE)')
        with contextlib.suppress(Exception):
            stop_postgres(pgdata)
        with contextlib.suppress(Exception):
            shutil.rmtree(workdir)
        raise

    print(json.dumps({**record, "admin_dsn": _masked(_dsn(record))}, ensure_ascii=False))
    return 0


# ---------------------------------------------------------------- 状态
def command_status(args: argparse.Namespace) -> int:
    record = load_instance(Path(args.workdir).resolve())
    with psycopg.connect(_dsn(record, kind="admin")) as conn:
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
        app_flags = conn.execute(
            "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = %s",
            (record["app_role"],),
        ).fetchone()
        owner = conn.execute(
            "SELECT tableowner FROM pg_tables WHERE tablename = 'knowledge_versions'"
        ).fetchone()
    print(json.dumps({
        "name": record["name"],
        "api_port": record["api_port"],
        "db_port": record["db_port"],
        "tenant_id": record["tenant_id"],
        "counts": counts,
        "versions": {str(s): int(c) for s, c in versions},
        "app_role": {
            "name": record["app_role"],
            "rolsuper": bool(app_flags[0]) if app_flags else None,
            "rolbypassrls": bool(app_flags[1]) if app_flags else None,
            "table_owner": owner[0] if owner else None,
        },
    }, ensure_ascii=False))
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
    provision.add_argument("--reuse", action="store_true",
                           help="已初始化时幂等复用原实例（保留租户/主体/数据/令牌）")
    provision.set_defaults(func=command_provision)

    serve = sub.add_parser("serve", help="用业务角色启动 API（校验密钥与实例身份）")
    serve.add_argument("--workdir", required=True)
    serve.add_argument("--wait-seconds", default="120")
    serve.set_defaults(func=command_serve)

    stop = sub.add_parser("stop", help="校验进程身份后停止 API 与内嵌 PG")
    stop.add_argument("--workdir", required=True)
    stop.set_defaults(func=command_stop)

    backup = sub.add_parser("backup", help="pg_dump 备份并自检（不含属主/授权/口令）")
    backup.add_argument("--workdir", required=True)
    backup.add_argument("--out", required=True)
    backup.set_defaults(func=command_backup)

    restore = sub.add_parser("restore", help="恢复到全新隔离实例（单事务，失败即清理）")
    restore.add_argument("--from", dest="from_dump", required=True)
    restore.add_argument("--workdir", required=True)
    restore.add_argument("--db-port", type=int, required=True)
    restore.add_argument("--api-port", type=int, required=True)
    restore.add_argument("--db-name", required=True)
    restore.add_argument("--name", default="restored")
    restore.set_defaults(func=command_restore)

    status = sub.add_parser("status", help="查看实例数据面（计数/版本/角色权限）")
    status.add_argument("--workdir", required=True)
    status.set_defaults(func=command_status)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
