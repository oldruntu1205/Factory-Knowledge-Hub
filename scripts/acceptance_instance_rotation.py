"""实例密钥轮换验收（2026-11-09）。

验证：
1. `rotate-keys` 替换 JWT 当前密钥与 DB 应用口令；
2. 宽限期内旧 JWT 仍可验签（旧令牌 200），新 JWT 也可用；
3. DB 旧口令立即失效、新口令可登录；
4. `retire-jwt-previous` 后重启，旧令牌 401、新令牌 200；
5. 新/旧密钥文件仍是私有 ACL，`instance.json` 不含密钥。

只使用临时隔离实例；工厂/烘焙演示环境不触碰。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlunparse

import jwt
import psycopg

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from fkh.tools import winacl  # noqa: E402

BASE = ROOT / ".acceptance"
EVIDENCE = ROOT / "_evidence" / "instance_rotation_acceptance.md"
CFG = {
    "workdir": BASE / "rotate-a",
    "db_port": 55499,
    "api_port": 8169,
    "db_name": "fkh_rotate_a",
    "name": "rotate-a",
}
SERVE_ENV = {
    "FKH_RETRIEVAL_MODE": "lexical",
    "PYTHONIOENCODING": "utf-8",
}
RECORDS: list[dict[str, Any]] = []


def step(name: str, ok: bool, detail: str = "") -> None:
    RECORDS.append({"step": name, "ok": ok, "detail": detail})
    print(json.dumps({"step": name, "ok": ok, "detail": detail}, ensure_ascii=False))


def run(args: list[str], *, expect_fail: bool = False) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, **SERVE_ENV}
    result = subprocess.run(
        args, cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=900, env=env,
    )
    RECORDS.append({
        "cmd": " ".join(args), "exit": result.returncode,
        "out": (result.stdout or "").strip().splitlines()[-2:],
        "err": (result.stderr or "").strip().splitlines()[-2:],
    })
    if result.returncode != 0 and not expect_fail:
        print(result.stdout, file=sys.stderr)
        print(result.stderr, file=sys.stderr)
        raise SystemExit(f"命令失败（{result.returncode}）：{' '.join(args)}")
    return result


def instance_cmd(*args: str, **kwargs: Any) -> subprocess.CompletedProcess[str]:
    return run([sys.executable, "-m", "fkh.tools.instance", *args], **kwargs)


def load() -> dict[str, Any]:
    return json.loads((CFG["workdir"] / "instance.json").read_text(encoding="utf-8"))


def secret(kind: str) -> str:
    names = {"jwt": "jwt.secret", "previous": "jwt.previous.secret", "app": "db-app.password"}
    return (CFG["workdir"] / "secrets" / names[kind]).read_text(encoding="utf-8").strip()


def token(secret_value: str, record: dict[str, Any]) -> str:
    now = datetime.now(UTC)
    return jwt.encode(
        {
            "iss": "fkh.local", "aud": "fkh-api", "sub": "rotation@local",
            "iat": now, "exp": now + timedelta(hours=3),
            "tenant_id": record["tenant_id"], "principal_id": record["ops_principal"],
            "roles": ["staff"], "scope": ["knowledge.read"],
            "allowed_scopes": ["internal_product", "customer_public"],
        },
        secret_value,
        algorithm="HS256",
    )


def app_dsn(record: dict[str, Any], password: str) -> str:
    return urlunparse(
        (
            "postgresql",
            f"{quote(record['app_role'], safe='')}:{quote(password, safe='')}@127.0.0.1:"
            f"{record['db_port']}",
            "/" + record["db_name"], "", "", "",
        )
    )


def app_login(record: dict[str, Any], password: str) -> bool:
    try:
        with psycopg.connect(app_dsn(record, password), connect_timeout=5) as conn:
            conn.execute("SELECT 1")
        return True
    except psycopg.Error:
        return False


def domains_http(record: dict[str, Any], token_value: str) -> int:
    request = urllib.request.Request(
        f"http://127.0.0.1:{record['api_port']}/v1/config/domains",
        headers={"Authorization": f"Bearer {token_value}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code


def write_evidence() -> None:
    lines = [
        "# 实例密钥轮换验收（JWT 宽限期 + DB 应用口令）",
        "",
        f"- 时间：{datetime.now(UTC).isoformat()}",
        "- 本记录不含任何密钥/口令内容；只记录状态、路径、退出码。",
        "",
        "| 步骤 | 结果 | 细节 |",
        "|---|---|---|",
    ]
    for item in RECORDS:
        if "step" in item:
            mark = "✅" if item["ok"] else "❌"
            lines.append(f"| {item['step']} | {mark} | {item['detail']} |")
    lines += ["", "## 命令（退出码）", "", "```text"]
    for item in RECORDS:
        if "cmd" in item:
            lines.append(f"$ {item['cmd']}  → exit={item['exit']}")
            for line in item.get("out", []):
                lines.append(f"  out: {line}")
            for line in item.get("err", []):
                lines.append(f"  err: {line}")
    lines += [
        "```", "", "## 语义", "",
        "- `rotate-keys`：旧 JWT 密钥保留为 `jwt.previous.secret`（只验签），新密钥写入 `jwt.secret`；",
        "  DB 应用角色口令立即更新，旧口令失效；要求 API 已停止。",
        "- `retire-jwt-previous`：删除旧 JWT 密钥；下次启动后旧令牌 401。",
        "- 管理员 DB 口令不在本命令轮换。",
        "",
    ]
    EVIDENCE.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    if CFG["workdir"].exists():
        if (CFG["workdir"] / "instance.json").exists():
            instance_cmd("stop", "--workdir", str(CFG["workdir"]), expect_fail=True)
        shutil.rmtree(CFG["workdir"], ignore_errors=True)
    time.sleep(1)
    instance_cmd(
        "provision", "--workdir", str(CFG["workdir"]), "--db-port", str(CFG["db_port"]),
        "--api-port", str(CFG["api_port"]), "--db-name", CFG["db_name"],
        "--name", CFG["name"],
    )
    record = load()
    instance_cmd("serve", "--workdir", str(CFG["workdir"]))

    old_jwt = secret("jwt")
    old_app = secret("app")
    old_token = token(old_jwt, record)
    step("old-token-before-rotation", domains_http(record, old_token) == 200, "200")
    step("old-db-password-before-rotation", app_login(record, old_app), "可以登录")

    instance_cmd("stop", "--workdir", str(CFG["workdir"]))
    rotated = instance_cmd("rotate-keys", "--workdir", str(CFG["workdir"]))
    step("rotate-keys", rotated.returncode == 0,
         (rotated.stdout or "").strip()[:220])
    record = load()
    new_jwt = secret("jwt")
    previous_jwt = secret("previous")
    new_app = secret("app")
    step("jwt-current-changed", new_jwt != old_jwt, "")
    step("jwt-previous-keeps-old-secret", previous_jwt == old_jwt, "")
    step("db-app-password-changed", new_app != old_app, "")
    step("instance-json-has-key-ids",
         record.get("jwt_key_id") == "jwt-2" and record.get("retrieval_key_id", "").endswith("-r1"),
         json.dumps({"jwt_key_id": record.get("jwt_key_id"),
                     "retrieval_key_id": record.get("retrieval_key_id")}, ensure_ascii=False))
    step("previous-secret-acl-private", winacl.describe(
        CFG["workdir"] / "secrets" / "jwt.previous.secret")["protected"], "")
    step("instance-json-has-no-secret",
         all(name not in (CFG["workdir"] / "instance.json").read_text(encoding="utf-8")
             for name in ("jwt_secret", "db_password", "admin_password", "app_password")), "")

    instance_cmd("serve", "--workdir", str(CFG["workdir"]))
    record = load()
    step("old-token-during-grace", domains_http(record, old_token) == 200, "200")
    step("new-token-during-grace", domains_http(record, token(new_jwt, record)) == 200, "200")
    step("old-db-password-rejected", not app_login(record, old_app), "旧口令失败")
    step("new-db-password-works", app_login(record, new_app), "新口令成功")

    instance_cmd("stop", "--workdir", str(CFG["workdir"]))
    retired = instance_cmd("retire-jwt-previous", "--workdir", str(CFG["workdir"]))
    step("retire-jwt-previous", retired.returncode == 0,
         (retired.stdout or "").strip()[:120])
    step("previous-file-removed",
         not (CFG["workdir"] / "secrets" / "jwt.previous.secret").exists(), "")

    instance_cmd("serve", "--workdir", str(CFG["workdir"]))
    record = load()
    step("old-token-after-retire", domains_http(record, old_token) == 401, "401")
    step("new-token-after-retire", domains_http(record, token(new_jwt, record)) == 200, "200")

    instance_cmd("stop", "--workdir", str(CFG["workdir"]))
    write_evidence()
    shutil.rmtree(CFG["workdir"], ignore_errors=True)
    return 0 if all(item["ok"] for item in RECORDS if "step" in item) else 1


if __name__ == "__main__":
    raise SystemExit(main())
