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
import re
import secrets
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse, urlunparse

import psycopg

from fkh.domain.catalog_rules import CatalogRuleError, compile_rule
from fkh.domain.domains import CODE_PATTERN
from fkh.retrieval.stats import refresh_tenant
from fkh.tools import winacl
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

SECRET_FILES = {
    "jwt": "jwt.secret",
    "jwt_previous": "jwt.previous.secret",
    "db_admin": "db-admin.password",
    "db_app": "db-app.password",
}


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
    # 元数据也要原子更新：轮换失败时不能留下半写的 instance.json
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)
        raise


def harden_secrets(workdir: Path) -> list[str]:
    """把 `secrets/` 目录与其中每个密钥文件收紧到「仅当前账户」。

    Windows 上 `chmod 0600` 不改变 DACL；如果只做 chmod，普通账户仍能读到密钥。
    这里先收紧目录、再收紧文件，任何一步不达标都会抛错，调用方不得继续启动。
    """
    secret_dir = workdir / "secrets"
    if not secret_dir.exists():
        raise SystemExit(f"缺少密钥目录：{secret_dir}")
    winacl.tighten(secret_dir, directory=True)
    hardened: list[str] = []
    for path in sorted(secret_dir.iterdir()):
        if path.is_file():
            winacl.tighten(path, directory=False)
            hardened.append(str(path))
    return hardened


def _write_secret(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # 先收紧目录：文件创建时会继承目录的 DACL，保证"写入时"就不对普通账户开放，
    # 而不是等写完再补救（中间窗口同样会泄露密钥）。
    winacl.tighten(path.parent, directory=True)
    path.write_text(value + "\n", encoding="utf-8")
    with contextlib.suppress(OSError):
        os.chmod(path, 0o600)
    winacl.tighten(path, directory=False)


def _read_secret(workdir: Path, kind: str, *, validate: bool = True) -> str:
    path = secret_path(workdir, kind)
    if not path.exists():
        raise SystemExit(f"缺少实例密钥文件：{path}（实例损坏或被手工删除，拒绝启动）")
    # 读取前自愈：老实例可能只有 chmod。若当前账户不是所有者，这里会失败并拒绝启动。
    winacl.tighten(path, directory=False)
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


def _args_have_port(args: list[str], port: int) -> bool:
    """命令行是否显式带 `--port <port>`（避免把路径里的数字误当端口）。"""
    expected = str(port)
    for index, token in enumerate(args):
        if token in {"--port", "-p"} and index + 1 < len(args) and args[index + 1] == expected:
            return True
        if token in {f"--port={expected}", f"-p{expected}"}:
            return True
    return False


def _is_this_app(args: list[str]) -> bool:
    normalized = [item.replace("\\", "/") for item in args]
    return any("uvicorn" in item for item in normalized) and any(
        "fkh.api.main:app" in item for item in normalized
    )


@dataclass(frozen=True, slots=True)
class _ProcessVerdict:
    pid: int
    state: str  # absent / ours / foreign / denied
    cmdline: str
    create_time: float | None = None


def _env_matches_instance(proc: Any, *, db_port: int, db_name: str) -> bool | None:
    """用子进程环境里的 FKH_DATABASE_URL 证明它连的是本实例的库。

    旧 pid 文件（纯整数）没有 nonce，不能只凭"同 app + 同端口"放行——同机上
    另一个用同一份代码/同租户的实例也可能命中。数据库端口 + 库名是本实例独有的。
    返回 None 表示环境不可读（无法证明，调用方应拒绝）。
    """
    import psutil

    try:
        env = proc.environ()
    except (psutil.AccessDenied, OSError):
        return None
    value = env.get("FKH_DATABASE_URL")
    if not value:
        return False
    try:
        parsed = urlparse(value)
        port = parsed.port
    except ValueError:
        return False
    return port == db_port and (parsed.path or "").lstrip("/") == db_name


def _classify_api_process(
    pid: int, *, api_port: int, db_port: int, db_name: str, nonce: str | None = None
) -> _ProcessVerdict:
    """判定 pid 是否**本实例的 API 进程**。

    - `absent`：进程已不存在（pid 文件是上次异常退出留下的）；
    - `ours`：身份可证（nonce + 本 app + 本库，或旧的 app/端口/监听者 + 本库）；
    - `foreign`：进程存在但不是本实例（**绝不终止**）；
    - `denied`：无权限读取，无法证明身份（保守拒绝）。

    为什么不再用「命令行包含 uvicorn 就算本实例」：另一个 uvicorn 服务
    （别的项目、别的端口）同样含 uvicorn，旧判定会把别人的服务杀掉。
    """
    import psutil

    try:
        proc = psutil.Process(pid)
        if not proc.is_running():
            return _ProcessVerdict(pid, "absent", "")
        args = proc.cmdline()
        create_time = float(proc.create_time())
    except psutil.NoSuchProcess:
        return _ProcessVerdict(pid, "absent", "")
    except psutil.AccessDenied:
        return _ProcessVerdict(pid, "denied", "无权限读取进程信息")

    normalized = [item.replace("\\", "/") for item in args]
    cmdline = " ".join(normalized)
    is_this_app = _is_this_app(args)

    if nonce:
        # 新格式：nonce 必须可读且命中；不命中/不可读一律不猜。
        try:
            env_nonce = proc.environ().get("FKH_INSTANCE_PID_NONCE")
        except (psutil.AccessDenied, OSError):
            return _ProcessVerdict(pid, "denied", "无权限读取进程环境（nonce 不可证）")
        if env_nonce != nonce or not is_this_app:
            return _ProcessVerdict(pid, "foreign", cmdline)
        if _env_matches_instance(proc, db_port=db_port, db_name=db_name) is not True:
            return _ProcessVerdict(pid, "foreign", cmdline)
        return _ProcessVerdict(pid, "ours", cmdline, create_time)

    if not (is_this_app and _args_have_port(normalized, api_port)):
        return _ProcessVerdict(pid, "foreign", cmdline)
    env_match = _env_matches_instance(proc, db_port=db_port, db_name=db_name)
    if env_match is None:
        return _ProcessVerdict(pid, "denied", "无权限读取进程环境（实例库不可证）")
    if not env_match:
        return _ProcessVerdict(pid, "foreign", cmdline)

    owner = port_owner(api_port)
    if owner is None:
        return _ProcessVerdict(pid, "foreign", f"{cmdline} [端口 {api_port} 当前无监听者]")
    if owner.get("pid") == pid:
        return _ProcessVerdict(pid, "ours", cmdline, create_time)
    # `.venv\Scripts\python.exe` 这类启动器会再起一个真实解释器子进程，
    # 监听端口的是子进程。允许 pid 是"本实例监听者的父进程"，但监听者本身
    # 也必须是本 app（否则就是不认识的进程占着端口）。
    try:
        listener_args = psutil.Process(owner["pid"]).cmdline()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return _ProcessVerdict(
            pid, "foreign", f"{cmdline} [端口 {api_port} 的监听者: {owner}]"
        )
    if _is_this_app(listener_args):
        with contextlib.suppress(psutil.Error):
            child_parents = {parent.pid for parent in psutil.Process(owner["pid"]).parents()}
            if pid in child_parents:
                return _ProcessVerdict(pid, "ours", cmdline, create_time)
    return _ProcessVerdict(pid, "foreign", f"{cmdline} [端口 {api_port} 的监听者: {owner}]")


def _read_pid_file(path: Path) -> dict[str, Any]:
    """读取 pid 文件；兼容旧的纯整数格式。"""
    raw = path.read_text(encoding="utf-8").strip()
    if not raw:
        return {"pid": 0, "nonce": None, "launcher_pid": None, "workdir": ""}
    if raw.startswith("{"):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"pid 文件内容非法：{path}") from exc
        if not isinstance(data, dict):
            raise SystemExit(f"pid 文件内容非法：{path}")
        try:
            pid = int(data.get("pid") or 0)
            launcher = int(data.get("launcher_pid") or 0)
        except (TypeError, ValueError) as exc:
            raise SystemExit(f"pid 文件内容非法：{path}") from exc
        nonce = data.get("nonce")
        return {
            "pid": pid,
            "nonce": str(nonce) if nonce else None,
            "launcher_pid": launcher or None,
            "workdir": str(data.get("workdir") or ""),
        }
    try:
        return {"pid": int(raw), "nonce": None, "launcher_pid": None, "workdir": ""}
    except ValueError as exc:
        raise SystemExit(f"pid 文件内容非法：{path}") from exc


# ---------------------------------------------------------------- 建库与角色
def _exec(dsn: str, sql: str) -> None:
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(sql)


def _database_exists(admin_dsn: str, db_name: str) -> bool:
    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        row = conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (db_name,)).fetchone()
    return row is not None


def _create_database(
    admin_dsn: str, db_name: str, *, on_created: Callable[[], None] | None = None
) -> None:
    if _database_exists(admin_dsn, db_name):
        raise SystemExit(f"数据库 {db_name} 已存在；restore/provision 只接受全新隔离目标")
    _exec(admin_dsn, f'CREATE DATABASE "{db_name}"')
    if on_created is not None:
        on_created()


def _role_exists(admin_dsn: str, role: str) -> bool:
    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        row = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone()
    return row is not None


def _role_name(db_name: str) -> str:
    return f"fkh_app_{db_name.replace('fkh_', '')[:16]}"


def _ensure_business_role(
    db_root_dsn: str,
    db_dsn: str,
    role: str,
    password: str,
    *,
    on_created: Callable[[], None] | None = None,
    require_new: bool = True,
) -> bool:
    """业务角色：LOGIN + 独立口令，非 owner、非 SUPERUSER、无 BYPASSRLS。

    返回 `True` 表示角色由本次调用创建（失败清理只应删除本次创建的角色）。
    `on_created` 在 `CREATE ROLE` 成功后**立即**回调：后续 GRANT 失败时，调用方
    仍能知道"角色是本次创建的"并清理，而不是等函数返回。

    `require_new=True`（默认）时，角色已存在直接拒绝：绝不能把 restore/provision
    的目标角色误当成"可复用的旧角色"去改它的口令或授权。
    """
    raise_if_exists = "SELECT 1 FROM pg_roles WHERE rolname = %s"
    with psycopg.connect(db_root_dsn, autocommit=True) as conn:
        existing = conn.execute(raise_if_exists, (role,)).fetchone()
    created = existing is None
    if not created and require_new:
        raise SystemExit(
            f"角色 {role} 已存在；restore/provision 只接受全新目标，拒绝修改既有角色"
        )
    if created:
        _exec(db_root_dsn, f'CREATE ROLE "{role}" LOGIN')
        if on_created is not None:
            on_created()
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
    return created


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
def _provision_domains(specs: list[str] | None) -> tuple[tuple[str, str], ...]:
    """`--domain code=label`；未给时保留工厂/通用模板，给了就完全替换。"""
    if not specs:
        return DEFAULT_DOMAINS
    domains: list[tuple[str, str]] = []
    seen: set[str] = set()
    for spec in specs:
        code, sep, label = spec.partition("=")
        code = code.strip()
        label = label.strip()
        if not sep or not code or not label:
            raise SystemExit(f"--domain 格式必须是 code=label：{spec!r}")
        if not CODE_PATTERN.match(code):
            raise SystemExit(f"--domain code 必须小写字母开头、长度 2–64：{code!r}")
        if code in seen:
            raise SystemExit(f"--domain code 重复：{code}")
        seen.add(code)
        domains.append((code, label))
    return tuple(domains)


def _provision_currencies(value: str) -> list[str]:
    currencies = [item.strip().upper() for item in value.split(",") if item.strip()]
    if not currencies:
        raise SystemExit("--currencies 至少需要一个币种")
    for currency in currencies:
        if not re.fullmatch(r"[A-Z]{3}", currency):
            raise SystemExit(f"币种必须是 3 位大写字母：{currency!r}")
    return currencies


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

    domains = _provision_domains(args.domain)
    catalog_pattern = args.catalog_pattern.strip()
    try:
        re.compile(catalog_pattern)
    except re.error as exc:
        raise SystemExit(f"--catalog-pattern 不是合法正则：{exc}") from exc
    try:
        rule = compile_rule(catalog_pattern, _provision_currencies(args.currencies))
    except CatalogRuleError as exc:
        raise SystemExit(f"目录规则非法：{exc}") from exc
    catalog_pattern = rule.code_pattern
    currencies = list(rule.currencies)

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

    created_at = datetime.now(UTC).isoformat()
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
        "domains": [code for code, _ in domains],
        "catalog_pattern": catalog_pattern,
        "currencies": currencies,
        "created_at": created_at,
        # 密钥元数据：只放标识与时间，不放任何密钥/Toke
        "jwt_key_id": "jwt-1",
        "retrieval_key_id": f"k-{args.name[:8]}",
        "jwt_rotated_at": created_at,
        "db_app_rotated_at": created_at,
        "rotation_seq": 0,
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
        for code, label in domains:
            conn.execute(
                "INSERT INTO knowledge_domains (tenant_id, code, label, is_default)"
                " VALUES (%s, %s, %s, true) ON CONFLICT DO NOTHING",
                (tenant, code, label),
            )
        conn.execute(
            "INSERT INTO catalog_rules (tenant_id, code_pattern, currencies)"
            " VALUES (%s, %s, %s) ON CONFLICT (tenant_id) DO NOTHING",
            (tenant, catalog_pattern, currencies),
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
def _domains_cover(expected: Iterable[str], actual: Iterable[str]) -> bool:
    """重启身份探测：初始清单必须是实际清单的子集（自定义域是追加的）。"""
    return set(expected) <= set(actual)


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
    # 自定义域是**追加**能力（POST /v1/config/domains），登记后数据库里的域
    # 会比 instance.json 的初始清单多。这里只要求初始清单一仍是子集：
    # 若用全等，登记自定义域后再重启会被误判成"应答者不是本实例"。
    # 身份仍由「本实例密钥签发的令牌 + 该租户数据可见」共同证明：换实例必 401。
    return _domains_cover(record.get("domains", []), codes)


def command_serve(args: argparse.Namespace) -> int:
    workdir = Path(args.workdir).resolve()
    record = load_instance(workdir)
    harden_secrets(workdir)  # 先确认密钥目录/文件只有本账户可读（老实例自愈）
    _assert_no_pending_secret_files(workdir)  # 轮换失败留下的 .new 不能带着未决状态启动
    secret = _read_secret(workdir, "jwt")  # 校验既有密钥（缺失/过短/占位直接拒绝）
    _refuse_if_busy(int(record["api_port"]), what="API")

    ensure_postgres(
        Path(record["pgdata"]), int(record["db_port"]),
        superuser_password=_read_secret(workdir, "db_admin"),
    )

    # 轮换宽限期：旧密钥只用于验签。文件不存在 = 已退役，旧令牌自然 401。
    previous_path = secret_path(workdir, "jwt_previous")
    previous = _read_secret(workdir, "jwt_previous") if previous_path.exists() else None

    # 进程身份随机串：只写进本次子进程环境与 pid 文件。
    # stop 用它证明"这个 pid 就是本次 serve 起来的 API"，而不是另一个 uvicorn 服务。
    pid_nonce = secrets.token_hex(16)
    env = dict(os.environ)
    env.update(
        {
            "FKH_JWT_SECRET": secret,
            "FKH_JWT_PREVIOUS_SECRETS": json.dumps([previous] if previous else []),
            "FKH_DATABASE_URL": _dsn(record, kind="app"),  # 口令只在环境变量里，不进 argv
            "FKH_RETRIEVAL_HASH_KEY": secret[:32],
            "FKH_RETRIEVAL_HASH_KEY_ID": str(
                record.get("retrieval_key_id") or f"k-{record['name'][:8]}"
            ),
            "FKH_INSTANCE_PID_NONCE": pid_nonce,
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

    if proc.poll() is not None:
        raise SystemExit(f"本次启动的 API 子进程已退出（code={proc.returncode}），日志：{log_path}")

    # `.venv\Scripts\python.exe` 是启动器，真正监听端口的是它的子进程；
    # pid 文件必须记录**监听者**，否则 stop 会停掉启动器而把服务进程留在后台。
    owner = port_owner(int(record["api_port"]))
    listener_pid = proc.pid
    if owner is not None and owner["pid"] != proc.pid:
        verdict = _classify_api_process(
            int(owner["pid"]),
            api_port=int(record["api_port"]),
            db_port=int(record["db_port"]),
            db_name=str(record["db_name"]),
            nonce=pid_nonce,
        )
        if verdict.state != "ours":
            proc.terminate()
            raise SystemExit(
                f"API 端口 {record['api_port']} 的监听者不是本次启动的实例（pid={owner['pid']}）："
                f"{verdict.cmdline[:160]}"
            )
        listener_pid = int(owner["pid"])

    (workdir / "api.pid").write_text(
        json.dumps(
            {
                "pid": listener_pid,
                "launcher_pid": proc.pid if proc.pid != listener_pid else None,
                "nonce": pid_nonce,
                "api_port": int(record["api_port"]),
                "workdir": str(workdir),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    print(json.dumps({"api": f"http://127.0.0.1:{record['api_port']}",
                      "pid": listener_pid, "ready": True, "instance": record["name"]}))
    return 0


def _terminate_process(verdict: _ProcessVerdict) -> None:
    """终止已被判定为 ours 的进程；再次核对 create_time，防止 PID 复用。

    判定与终止之间如果原进程退出、系统把同一 PID 分配给别的进程，只按 pid
    terminate 会杀错。create_time 是进程身份的一部分。
    """
    import psutil

    if verdict.state != "ours" or verdict.create_time is None:
        raise SystemExit(f"pid={verdict.pid} 未被证明属于本实例，拒绝终止")
    try:
        proc = psutil.Process(verdict.pid)
        current_create_time = float(proc.create_time())
    except psutil.NoSuchProcess:
        return
    except psutil.AccessDenied as exc:
        raise SystemExit(f"pid={verdict.pid} 无权限复核进程身份，拒绝终止") from exc
    if abs(current_create_time - verdict.create_time) > 0.01:
        raise SystemExit(
            f"pid={verdict.pid} 已被替换（create_time 变化），拒绝终止新的进程"
        )
    try:
        proc.terminate()
    except psutil.NoSuchProcess:
        return  # 复核后自行退出：已经是目标结果
    except psutil.AccessDenied as exc:
        raise SystemExit(f"pid={verdict.pid} 无权限终止，拒绝继续") from exc
    with contextlib.suppress(Exception):
        proc.wait(timeout=20)
    if proc.is_running():
        try:
            proc.kill()
        except psutil.NoSuchProcess:
            return
        except psutil.AccessDenied as exc:
            raise SystemExit(f"pid={verdict.pid} 无权限强制终止，拒绝继续") from exc
        with contextlib.suppress(Exception):
            proc.wait(timeout=5)


def command_stop(args: argparse.Namespace) -> int:
    workdir = Path(args.workdir).resolve()
    record = load_instance(workdir)
    api_port = int(record["api_port"])
    db_port = int(record["db_port"])
    db_name = str(record["db_name"])
    pid_file = workdir / "api.pid"
    stopped_api = False
    if pid_file.exists():
        info = _read_pid_file(pid_file)
        pid = int(info["pid"] or 0)
        nonce = info["nonce"]
        launcher_pid = int(info["launcher_pid"] or 0)
        recorded_workdir = str(info["workdir"] or "")
        if recorded_workdir and Path(recorded_workdir).resolve() != workdir:
            raise SystemExit(
                f"pid 文件记录的实例目录是 {recorded_workdir}，不是 {workdir}；拒绝停止"
            )

        targets: list[_ProcessVerdict] = []

        def add(verdict: _ProcessVerdict) -> None:
            if verdict.state == "ours" and all(item.pid != verdict.pid for item in targets):
                targets.append(verdict)

        if pid:
            verdict = _classify_api_process(
                pid, api_port=api_port, db_port=db_port, db_name=db_name, nonce=nonce
            )
            if verdict.state in {"foreign", "denied"}:
                detail = verdict.cmdline[:200] or "无法读取命令行"
                raise SystemExit(
                    f"pid={pid} 不是本实例的 API 进程（{detail}），拒绝停止"
                )
            add(verdict)
            # state == absent：进程已不在，pid 文件是上次异常退出的残留，继续核验监听者

        # 监听者是"本实例是否还在服务"的直接证据；pid 文件丢失/陈旧时也要检查。
        owner = port_owner(api_port)
        if owner is not None:
            owner_verdict = _classify_api_process(
                int(owner["pid"]),
                api_port=api_port,
                db_port=db_port,
                db_name=db_name,
                nonce=nonce,
            )
            if owner_verdict.state != "ours":
                raise SystemExit(
                    f"API 端口 {api_port} 的监听者无法确认为本实例（pid={owner['pid']}），"
                    "拒绝停止"
                )
            # 监听者优先终止，避免启动器先退出后留下孤儿
            if all(item.pid != owner_verdict.pid for item in targets):
                targets.insert(0, owner_verdict)

        if launcher_pid and launcher_pid not in {item.pid for item in targets}:
            launcher_verdict = _classify_api_process(
                launcher_pid, api_port=api_port, db_port=db_port, db_name=db_name, nonce=nonce
            )
            # 启动器可能已被回收/复用；它只作为附加清理项，foreign 时不动它。
            if launcher_verdict.state == "ours":
                add(launcher_verdict)

        for target in targets:
            _terminate_process(target)
        stopped_api = bool(targets)
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


# ---------------------------------------------------------------- 密钥轮换
def _assert_api_stopped(workdir: Path, record: dict[str, Any]) -> None:
    """轮换要求 API 停止：否则旧进程仍持有旧密钥/旧口令，重启前状态不可解释。"""
    api_port = int(record["api_port"])
    owner = port_owner(api_port)
    if owner is not None:
        raise SystemExit(
            f"API 仍在端口 {api_port} 监听（pid={owner['pid']}）；请先 stop 再轮换"
        )
    pid_file = workdir / "api.pid"
    if pid_file.exists():
        info = _read_pid_file(pid_file)
        pid = int(info["pid"] or 0)
        if pid:
            verdict = _classify_api_process(
                pid, api_port=api_port, db_port=int(record["db_port"]),
                db_name=str(record["db_name"]), nonce=info["nonce"],
            )
            if verdict.state == "ours":
                raise SystemExit(f"本实例 API 进程 pid={pid} 仍在运行；请先 stop 再轮换")


PENDING_SECRET_SUFFIXES = (
    "jwt.secret.new",
    "jwt.previous.secret.new",
    "db-app.password.new",
)


def _pending_secret_files(workdir: Path) -> list[Path]:
    secrets_dir = workdir / "secrets"
    return [secrets_dir / name for name in PENDING_SECRET_SUFFIXES if (secrets_dir / name).exists()]


def _assert_no_pending_secret_files(workdir: Path) -> None:
    pending = _pending_secret_files(workdir)
    if pending:
        joined = "、".join(str(path) for path in pending)
        raise SystemExit(
            f"检测到待恢复的临时密钥文件：{joined}；先确认当前生效的密钥/口令并处理这些文件，"
            "再继续（不会自动覆盖）"
        )


class _PasswordChangeUncertain(RuntimeError):
    """ALTER ROLE 已发出但连接中断：服务端可能已经提交，不能假定旧口令仍有效。"""


def _alter_role_password(root_dsn: str, role: str, password: str) -> None:
    """在显式事务里改口令，区分"服务端拒绝"与"结果不确定"。

    - `DatabaseError`：服务端明确拒绝，事务未提交，可确认旧口令仍有效；
    - `OperationalError`/`InterfaceError`/其它连接类异常：可能提交后才断开，
      调用方必须保留新口令文件并报告"待确认"。
    """
    try:
        with psycopg.connect(root_dsn, autocommit=False) as conn:
            conn.execute(f'ALTER ROLE "{role}" PASSWORD {_sql_literal(password)}')
            conn.commit()
    except (psycopg.OperationalError, psycopg.InterfaceError) as exc:
        # 连接类异常可能发生在服务端提交之后：不能据此认定旧口令仍有效
        raise _PasswordChangeUncertain(str(exc)) from exc
    except psycopg.errors.DatabaseError as exc:
        # 服务端明确返回错误，事务未提交
        raise SystemExit(f"ALTER ROLE 被 PostgreSQL 拒绝：{exc}") from exc


def _atomic_write_secret(path: Path, value: str) -> None:
    """先写同目录 .new，再原子替换；替换失败时保留 .new 供人工恢复。"""
    tmp = path.with_name(path.name + ".new")
    _write_secret(tmp, value)
    try:
        os.replace(tmp, path)
    except OSError as exc:
        raise SystemExit(
            f"密钥已写入 {tmp}，但替换 {path} 失败：{exc}；请人工检查后重试"
        ) from exc


def _update_record(workdir: Path, record: dict[str, Any], **changes: Any) -> None:
    record.update(changes)
    _write_instance(workdir, record)


def command_rotate_keys(args: argparse.Namespace) -> int:
    """轮换 JWT 签名密钥与 DB 应用角色口令（要求 API 已停止）。

    - JWT：当前密钥写入 `jwt.previous.secret` 作为宽限期验签密钥，新密钥写入
      `jwt.secret`；旧令牌在下次重启后仍有效，直到 `retire-jwt-previous`。
    - DB：`ALTER ROLE <app> PASSWORD`，新口令原子替换 `db-app.password`；
      管理员口令不在此轮换（避免一次操作同时改两类运维凭据）。
    """
    workdir = Path(args.workdir).resolve()
    record = load_instance(workdir)
    _assert_api_stopped(workdir, record)
    _assert_no_pending_secret_files(workdir)
    if secret_path(workdir, "jwt_previous").exists() and not args.force:
        raise SystemExit(
            "宽限期旧 JWT 密钥仍在；先 retire-jwt-previous，"
            "或加 --force（会让更早签发的令牌提前失效）"
        )

    seq = int(record.get("rotation_seq") or 0) + 1
    now = datetime.now(UTC).isoformat()
    jwt_key_id = f"jwt-{seq + 1}"
    retrieval_key_id = f"k-{record['name'][:8]}-r{seq}"
    history = list(record.get("rotation_history") or [])

    def record_event(event: str, **fields: Any) -> None:
        history.append({"at": datetime.now(UTC).isoformat(), "event": event, **fields})
        del history[:-20]  # 只留最近 20 条，避免元数据无限增长

    # JWT：先保留旧密钥，再替换当前密钥。任一步失败都不会丢掉旧密钥。
    current_jwt = _read_secret(workdir, "jwt")
    _atomic_write_secret(secret_path(workdir, "jwt_previous"), current_jwt)
    new_jwt = _new_secret()
    _atomic_write_secret(secret_path(workdir, "jwt"), new_jwt)
    record_event("jwt_rotated", jwt_key_id=jwt_key_id, retrieval_key_id=retrieval_key_id)
    _update_record(
        workdir,
        record,
        rotation_seq=seq,
        jwt_key_id=jwt_key_id,
        retrieval_key_id=retrieval_key_id,
        jwt_rotated_at=now,
        rotation_history=history,
    )

    # DB 应用口令：先写 .new，ALTER 成功后再原子替换；API 已停止，无新连接使用旧口令。
    pgdata = Path(record["pgdata"])
    pg_was_running = postgres_status(pgdata)
    root_dsn = ensure_postgres(
        pgdata, int(record["db_port"]),
        superuser_password=_read_secret(workdir, "db_admin"),
    )
    new_app = _new_secret()
    app_path = secret_path(workdir, "db_app")
    tmp_app = app_path.with_name(app_path.name + ".new")
    _write_secret(tmp_app, new_app)
    try:
        _alter_role_password(root_dsn, str(record["app_role"]), new_app)
    except _PasswordChangeUncertain as exc:
        # 连接中断可能发生在服务端提交之后：保留新口令文件，不能删除、不能假装旧口令仍有效。
        record_event(
            "db_app_rotated_unconfirmed",
            pending_file=str(tmp_app),
            postgres_left_running=True,
        )
        _update_record(workdir, record, rotation_history=history)
        raise SystemExit(
            f"DB 口令变更结果待确认（连接中断，可能已生效）：{exc}；"
            f"新口令保存在 {tmp_app}（私有 ACL）。请先分别用旧/新口令测试登录，"
            "确认后再替换 db-app.password；在 pending 文件存在期间 rotate/serve 会拒绝继续"
        ) from exc
    except BaseException:
        tmp_app.unlink(missing_ok=True)
        if not pg_was_running:
            stop_postgres(pgdata)
        raise
    try:
        os.replace(tmp_app, app_path)
    except OSError as exc:
        record_event(
            "db_app_replace_failed",
            pending_file=str(tmp_app),
            postgres_left_running=pg_was_running,
        )
        _update_record(workdir, record, rotation_history=history)
        if not pg_was_running:
            stop_postgres(pgdata)
        raise SystemExit(
            f"数据库口令已更新，但替换 {app_path} 失败：{exc}；"
            f"新口令在 {tmp_app}（私有 ACL），请人工替换后重启"
        ) from exc

    # 用新口令实际登录一次，避免"文件换了但连不上"被当成成功
    try:
        with psycopg.connect(_dsn(record, kind="app"), connect_timeout=5) as conn:
            conn.execute("SELECT 1")
    except psycopg.Error as exc:
        record_event("db_app_verify_failed", postgres_left_running=pg_was_running)
        _update_record(workdir, record, rotation_history=history)
        if not pg_was_running:
            stop_postgres(pgdata)
        raise SystemExit(f"新 DB 口令无法登录，轮换未完成：{exc}") from exc
    record_event("db_app_rotated")
    _update_record(workdir, record, db_app_rotated_at=now, rotation_history=history)
    if not pg_was_running:
        stop_postgres(pgdata)
        if postgres_status(pgdata):
            raise SystemExit("轮换后内嵌 PostgreSQL 未能停止，请人工处理")

    print(json.dumps({
        "rotated": True,
        "jwt_key_id": jwt_key_id,
        "retrieval_key_id": retrieval_key_id,
        "previous_grace": True,
        "restart_required": True,
        "db_app_rotated": True,
    }, ensure_ascii=False))
    return 0


def command_retire_jwt_previous(args: argparse.Namespace) -> int:
    """删除宽限期旧 JWT 密钥；下次重启后旧令牌一律 401。要求 API 已停止。"""
    workdir = Path(args.workdir).resolve()
    record = load_instance(workdir)
    _assert_api_stopped(workdir, record)
    _assert_no_pending_secret_files(workdir)
    path = secret_path(workdir, "jwt_previous")
    existed = path.exists()
    path.unlink(missing_ok=True)
    history = list(record.get("rotation_history") or [])
    history.append({
        "at": datetime.now(UTC).isoformat(),
        "event": "jwt_previous_retired",
        "existed": existed,
    })
    del history[:-20]
    _update_record(
        workdir, record,
        jwt_previous_retired_at=datetime.now(UTC).isoformat(),
        rotation_history=history,
    )
    print(json.dumps({"retired": existed, "restart_required": True}, ensure_ascii=False))
    return 0


def command_refresh_stats(args: argparse.Namespace) -> int:
    """用实例管理员口令重建检索统计表（离线运维动作；REPEATABLE READ）。"""
    workdir = Path(args.workdir).resolve()
    record = load_instance(workdir)
    pgdata = Path(record["pgdata"])
    was_running = postgres_status(pgdata)
    root_dsn = ensure_postgres(
        pgdata, int(record["db_port"]),
        superuser_password=_read_secret(workdir, "db_admin"),
    )
    try:
        with psycopg.connect(root_dsn, autocommit=False) as conn:
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            summary = refresh_tenant(conn, uuid.UUID(str(record["tenant_id"])))
            conn.commit()
    except BaseException:
        if not was_running:
            stop_postgres(pgdata)
        raise
    if not was_running:
        stop_postgres(pgdata)
        if postgres_status(pgdata):
            raise SystemExit("统计刷新后内嵌 PostgreSQL 未能停止，请人工处理")
    print(json.dumps(summary, ensure_ascii=False))
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


def _cleanup_restore(
    *,
    workdir: Path,
    pgdata: Path,
    root_dsn: str | None,
    db_name: str,
    role: str,
    created_db: bool,
    created_role: bool,
    db_creation_attempted: bool,
    role_creation_attempted: bool,
    created_pgdata: bool,
    created_secrets: bool,
    instance_written: bool,
    workdir_created: bool,
) -> list[str]:
    """尽力撤销本次 restore 创建的资源；返回未能清理的项（不覆盖原始异常）。

    每个资源独立尝试；只把"资源不存在"视为正常。清理失败不 raise，由调用方
    打印/附注到原始异常上，避免"清理失败盖掉真正的失败原因"。
    """
    failures: list[str] = []

    def attempt(label: str, action: Callable[[], None]) -> None:
        try:
            action()
        except FileNotFoundError:
            return
        except Exception as exc:  # noqa: BLE001 - 清理必须继续尝试其余资源
            failures.append(f"{label}: {type(exc).__name__}: {exc}")

    if root_dsn is not None and created_db:
        attempt(
            "drop database",
            lambda: _exec(root_dsn, f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)'),
        )
    if root_dsn is not None and created_role:
        attempt("drop role", lambda: _exec(root_dsn, f'DROP ROLE IF EXISTS "{role}"'))
    # DDL 已发出但客户端没拿到结果（连接中断等）：数据库/角色可能已在服务端创建。
    # 此时不能仅凭名字删除（可能是并发创建/既有对象），只报告待人工核查。
    if db_creation_attempted and not created_db:
        failures.append(
            f"database {db_name} 创建结果未确认（可能已创建）；"
            "未自动删除，请人工核查"
        )
    if role_creation_attempted and not created_role:
        failures.append(
            f"role {role} 创建结果未确认（可能已创建）；未自动删除，请人工核查"
        )
    if created_pgdata and pgdata.exists():
        attempt("stop postgres", lambda: stop_postgres(pgdata))
        attempt("remove pgdata", lambda: shutil.rmtree(pgdata))
    if created_secrets:
        attempt("remove secrets", lambda: shutil.rmtree(workdir / "secrets"))
    if instance_written:
        attempt("remove instance.json", lambda: instance_path(workdir).unlink())
    if workdir_created:
        try:
            workdir.rmdir()
        except FileNotFoundError:
            pass
        except OSError as exc:
            failures.append(f"remove workdir（非空则保留）: {type(exc).__name__}: {exc}")
    return failures


def command_restore(args: argparse.Namespace) -> int:
    workdir = Path(args.workdir).resolve()
    dump = Path(args.from_dump).resolve()
    if not dump.exists():
        raise SystemExit(f"备份文件不存在：{dump}")
    if workdir.exists():
        if not workdir.is_dir():
            raise SystemExit(f"目标不是目录：{workdir}；restore 只接受全新目录")
        if any(workdir.iterdir()):
            raise SystemExit(
                f"目标目录非空：{workdir}；restore 只接受全新目录（不存在或为空目录）"
            )
    # 记录"目录由谁创建"：失败清理只删除本次创建的资源，不动调用方预先建好的空目录。
    workdir_created = not workdir.exists()
    _refuse_if_busy(args.api_port, what="API")
    _refuse_if_busy(args.db_port, what="DB")

    listed = run_pg([str(pg_bin() / "pg_restore.exe"), "--list", str(dump)])
    if listed.returncode != 0:
        raise SystemExit(f"备份文件不可读，拒绝恢复：\n{listed.stdout}\n{listed.stderr}")

    workdir.mkdir(parents=True, exist_ok=True)
    pgdata = (workdir / "pgdata").resolve()
    created_pgdata = not pgdata.exists()
    created_secrets = not (workdir / "secrets").exists()
    instance_written = False
    admin_pw, app_pw = _new_secret(), _new_secret()

    root_dsn: str | None = None
    created_db = False
    created_role = False
    db_creation_attempted = False
    role_creation_attempted = False
    role = _role_name(args.db_name)

    def _mark_db_created() -> None:
        nonlocal created_db
        created_db = True

    def _mark_role_created() -> None:
        nonlocal created_role
        created_role = True

    try:
        _write_secret(secret_path(workdir, "db_admin"), admin_pw)
        _write_secret(secret_path(workdir, "db_app"), app_pw)
        root_dsn = ensure_postgres(pgdata, args.db_port, superuser_password=admin_pw)
        db_dsn = urlunparse(urlparse(root_dsn)._replace(path="/" + args.db_name))
        if _database_exists(root_dsn, args.db_name):
            raise SystemExit(f"数据库 {args.db_name} 已存在；restore 只接受全新隔离目标")
        db_creation_attempted = True
        try:
            _create_database(root_dsn, args.db_name, on_created=_mark_db_created)
        except SystemExit:
            # `_create_database` 的 SystemExit 只表示"库已存在"（不是本次创建）。
            db_creation_attempted = False
            raise
        if _role_exists(root_dsn, role):
            # 恢复只接受全新目标：角色若已存在，可能属于别的实例，不能改它的口令。
            raise SystemExit(f"角色 {role} 已存在；restore 只接受全新隔离目标")
        role_creation_attempted = True
        try:
            _ensure_business_role(
                root_dsn, db_dsn, role, app_pw,
                on_created=_mark_role_created, require_new=True,
            )
        except SystemExit:
            # 同上：角色已存在（竞态）不是本次创建。
            role_creation_attempted = False
            raise
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
        created_at = datetime.now(UTC).isoformat()
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
            "created_at": created_at,
            "jwt_key_id": "jwt-1",
            "retrieval_key_id": f"k-{args.name[:8]}",
            "jwt_rotated_at": created_at,
            "db_app_rotated_at": created_at,
            "rotation_seq": 0,
            "restored_from": str(dump),
            "tenant_id": tenant,
            "ops_principal": next((str(p[0]) for p in principals if str(p[1]) == "user"), None),
            "agent_principal": next((str(p[0]) for p in principals if str(p[1]) == "agent"), None),
        }
        _assert_auth_isolation(record)
        instance_written = True  # 先登记：write_text 中途失败也要清掉半写的 instance.json
        _write_instance(workdir, record)
    except BaseException as exc:
        # 半恢复清理：只撤销**本次**创建的库/角色/目录，不动目标目录里原本就有的东西。
        failures = _cleanup_restore(
            workdir=workdir,
            pgdata=pgdata,
            root_dsn=root_dsn,
            db_name=args.db_name,
            role=role,
            created_db=created_db,
            created_role=created_role,
            db_creation_attempted=db_creation_attempted,
            role_creation_attempted=role_creation_attempted,
            created_pgdata=created_pgdata,
            created_secrets=created_secrets,
            instance_written=instance_written,
            workdir_created=workdir_created,
        )
        for failure in failures:
            print(f"restore 清理未完成：{failure}", file=sys.stderr)
            if hasattr(exc, "add_note"):
                exc.add_note(f"restore 清理未完成：{failure}")
        raise

    print(json.dumps({**record, "admin_dsn": _masked(_dsn(record))}, ensure_ascii=False))
    return 0


def command_harden_secrets(args: argparse.Namespace) -> int:
    """重新收紧既有实例的 secrets/（用于旧实例升级到 DACL 保护）。"""
    workdir = Path(args.workdir).resolve()
    hardened = harden_secrets(workdir)
    print(json.dumps({"hardened": True, "secret_dir": str(workdir / "secrets"),
                      "files": hardened}, ensure_ascii=False))
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
        stats_info: dict[str, Any] | None = None
        try:
            with conn.transaction():
                row = conn.execute(
                    """SELECT status, source_revision, built_revision, generation,
                              refreshed_at, last_error
                         FROM retrieval_stats_state WHERE tenant_id = %s""",
                    (record["tenant_id"],),
                ).fetchone()
            if row is None:
                stats_info = {"status": "missing"}
            else:
                stats_info = {
                    "status": str(row[0]),
                    "source_revision": int(row[1]),
                    "built_revision": int(row[2]) if row[2] is not None else None,
                    "generation": int(row[3]),
                    "refreshed_at": row[4].isoformat() if row[4] else None,
                    "last_error": row[5],
                }
        except psycopg.errors.UndefinedTable:
            stats_info = {"status": "unavailable（先应用迁移 0037）"}
    print(json.dumps({
        "name": record["name"],
        "api_port": record["api_port"],
        "db_port": record["db_port"],
        "tenant_id": record["tenant_id"],
        "counts": counts,
        "versions": {str(s): int(c) for s, c in versions},
        "retrieval_stats": stats_info,
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
    provision.add_argument(
        "--domain", action="append", metavar="CODE=LABEL",
        help="实例知识域，可重复；不写则用通用四域模板",
    )
    provision.add_argument(
        "--catalog-pattern", default=DEFAULT_CATALOG_PATTERN,
        help="目录货号正则（默认 %(default)s）",
    )
    provision.add_argument(
        "--currencies", default=",".join(DEFAULT_CURRENCIES),
        help="目录允许币种，逗号分隔（默认 %(default)s）",
    )
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

    harden_cmd = sub.add_parser(
        "harden-secrets", help="把既有实例的 secrets/ 收紧为仅当前账户可读（Windows DACL）"
    )
    harden_cmd.add_argument("--workdir", required=True)
    harden_cmd.set_defaults(func=command_harden_secrets)

    rotate = sub.add_parser(
        "rotate-keys", help="轮换 JWT 密钥与 DB 应用口令（要求 API 已停止；JWT 旧密钥进入宽限期）"
    )
    rotate.add_argument("--workdir", required=True)
    rotate.add_argument(
        "--force", action="store_true",
        help="宽限期旧密钥仍在时仍轮换（会使更早签发的令牌提前失效）",
    )
    rotate.set_defaults(func=command_rotate_keys)

    retire = sub.add_parser(
        "retire-jwt-previous", help="删除宽限期的旧 JWT 密钥；下次重启后旧令牌 401"
    )
    retire.add_argument("--workdir", required=True)
    retire.set_defaults(func=command_retire_jwt_previous)

    refresh = sub.add_parser(
        "refresh-stats", help="用管理员口令重建检索统计表（离线运维；REPEATABLE READ）"
    )
    refresh.add_argument("--workdir", required=True)
    refresh.set_defaults(func=command_refresh_stats)

    status = sub.add_parser("status", help="查看实例数据面（计数/版本/角色权限）")
    status.add_argument("--workdir", required=True)
    status.set_defaults(func=command_status)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
