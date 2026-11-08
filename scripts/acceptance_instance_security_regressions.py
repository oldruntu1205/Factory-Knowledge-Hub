"""客户实例安全回归验收（2026-11-09）。

覆盖四项：
1. Windows DACL 保护实例密钥，并用**临时普通账户**实证读不到；
2. restore 只接受全新目录，失败时只清理本次创建的资源；
3. 两个回归：自定义域登记后仍能重启；pid 文件指向其他服务时拒绝停止且对方仍在运行；
4. 冷启动后首个有效 `POST /v1/knowledge/{id}/review` 必须 200（不再靠重试）。

只使用新建隔离实例与合成资料；工厂/烘焙演示环境不触碰。
"""

from __future__ import annotations

import argparse
import base64
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

import jwt
import psutil

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from fkh.tools import winacl  # noqa: E402
from fkh.tools.pg import pg_bin  # noqa: E402

BASE = ROOT / ".acceptance"
EVIDENCE = ROOT / "_evidence" / "instance_security_regressions.md"

A = {
    "workdir": BASE / "sec-a",
    "db_port": 55495,
    "api_port": 8165,
    "db_name": "fkh_sec_a",
    "name": "sec-a",
}
RESTORE_PORTS = {"db_port": 55496, "api_port": 8166}
FOREIGN_PORT = 8199

SERVE_ENV = {
    "FKH_RETRIEVAL_MODE": "lexical",
    "FKH_LLM_PROVIDER": "ollama",
    "FKH_LLM_MODEL": "qwen3:14b",
    "FKH_LLM_BASE_URL": "http://127.0.0.1:11434",
    "FKH_LLM_TIMEOUT_SECONDS": "180",
    "PYTHONIOENCODING": "utf-8",
}
OPS_SCOPES = (
    "knowledge.read", "knowledge.write", "knowledge.ingest", "knowledge.review",
    "knowledge.publish", "knowledge.revoke", "knowledge.answer", "knowledge.search",
    "evidence.read",
)
RECORDS: list[dict[str, Any]] = []
RUN_TAG = datetime.now(UTC).strftime("%H%M%S")


def step(name: str, ok: bool, detail: str = "") -> None:
    RECORDS.append({"step": name, "ok": ok, "detail": detail})
    print(json.dumps({"step": name, "ok": ok, "detail": detail}, ensure_ascii=False))


def run(
    args: list[str],
    *,
    timeout: int = 900,
    env_extra: dict[str, str] | None = None,
    expect_fail: bool = False,
) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, **SERVE_ENV, **(env_extra or {})}
    result = subprocess.run(
        args,
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        env=env,
    )
    RECORDS.append({
        "cmd": " ".join(args),
        "exit": result.returncode,
        "out": (result.stdout or "").strip().splitlines()[-3:],
        "err": (result.stderr or "").strip().splitlines()[-3:],
    })
    if not expect_fail and result.returncode != 0:
        print(result.stdout, file=sys.stderr)
        print(result.stderr, file=sys.stderr)
        raise SystemExit(f"命令失败（{result.returncode}）：{' '.join(args)}")
    return result


def instance_cmd(*args: str, **kwargs: Any) -> subprocess.CompletedProcess[str]:
    return run([sys.executable, "-m", "fkh.tools.instance", *args], **kwargs)


def http(
    method: str,
    url: str,
    *,
    payload: dict[str, Any] | None = None,
    token: str | None = None,
    timeout: int = 120,
) -> tuple[int, dict[str, Any]]:
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"Content-Type": "application/json"} if data is not None else {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode() or "{}"
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            parsed = {"raw": body[:200]}
        return exc.code, parsed


def load(workdir: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((workdir / "instance.json").read_text(encoding="utf-8"))
    return data


def jwt_secret(workdir: Path) -> str:
    return (workdir / "secrets" / "jwt.secret").read_text(encoding="utf-8").strip()


def mint(workdir: Path, record: dict[str, Any]) -> str:
    now = datetime.now(UTC)
    return jwt.encode(
        {
            "iss": "fkh.local", "aud": "fkh-api", "sub": "acceptance@security.local",
            "iat": now, "exp": now + timedelta(hours=3),
            "tenant_id": record["tenant_id"], "principal_id": record["ops_principal"],
            "roles": ["staff"], "scope": list(OPS_SCOPES),
            "allowed_scopes": ["internal_product", "customer_public"],
        },
        jwt_secret(workdir),
        algorithm="HS256",
    )


def api(record: dict[str, Any], path: str) -> str:
    return f"http://127.0.0.1:{record['api_port']}{path}"


def upload(
    record: dict[str, Any], token: str, *, title: str, text: str, domain: str, key: str
) -> tuple[str, int, int]:
    """上传并**立即**审核；返回 (version_id, upload_code, review_code)，不做重试。"""
    body = {
        "filename": f"{key}.md", "title": title, "domain": domain,
        "owner_department": "ops", "classification": 1, "audience": "internal",
        "required_scope": "internal_product", "acl_allow_roles": ["staff"],
        "knowledge_key": key,
        "content_base64": base64.b64encode(text.encode()).decode(),
    }
    code, payload = http("POST", api(record, "/v1/knowledge/documents"), payload=body, token=token)
    if code != 201:
        raise SystemExit(f"导入失败 {code}: {payload}")
    version = str(payload["version_id"])
    review_code, review_body = http(
        "POST", api(record, f"/v1/knowledge/{version}/review"),
        payload={"decision": "PASS"}, token=token,
    )
    if review_code != 200:
        step("review-first-request", False, f"key={key} 首次 review={review_code} {review_body}")
    return version, code, review_code


def port_listening(port: int) -> bool:
    import socket

    with socket.socket() as sock:
        sock.settimeout(0.3)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def cleanup_leftover_ports(ports: tuple[int, ...]) -> None:
    for port in ports:
        for conn in psutil.net_connections(kind="tcp"):
            if conn.status != psutil.CONN_LISTEN or not conn.laddr or conn.laddr.port != port:
                continue
            pid = conn.pid or 0
            if not pid:
                continue
            try:
                cmdline = " ".join(psutil.Process(pid).cmdline()).replace("\\", "/")
            except Exception:
                cmdline = ""
            if ".acceptance" not in cmdline and "fkh.api.main" not in cmdline:
                raise SystemExit(f"端口 {port} 被非本验收进程占用（pid={pid}）：{cmdline[:120]}")
            psutil.Process(pid).terminate()


def _read_ps_redirect(path: Path) -> str:
    raw = path.read_bytes()
    if raw.startswith(b"\xff\xfe"):
        return raw.decode("utf-16-le").lstrip("\ufeff")
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw.decode("utf-8-sig")
    return raw.decode("utf-8", errors="replace")


def _write_ps1(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8-sig")


def standard_user_probe(secret_dir: Path, control: Path) -> dict[str, Any]:
    """创建一个临时普通账户，让它尝试读密钥；返回 {"user":..., "probe":...}。

    用户与临时脚本在 finally 中删除；失败清理不影响其它账户。
    """
    import secrets as pysecrets

    user = f"fkh_acl_{pysecrets.token_hex(4)}"
    password = f"Fkh!{pysecrets.token_urlsafe(12)}aA9"
    probe_path = BASE / "acl-probe.ps1"
    wrapper = BASE / "acl-wrapper.ps1"
    out_file = BASE / "acl-probe.out"
    err_file = BASE / "acl-probe.err"
    files = [
        secret_dir / "jwt.secret",
        secret_dir / "db-admin.password",
        secret_dir / "db-app.password",
    ]
    file_literals = ",".join(f"'{p.as_posix()}'" for p in files)
    _write_ps1(
        probe_path,
        "\n".join([
            "$ErrorActionPreference='Continue'",
            f"$files=@({file_literals})",
            "$r=[ordered]@{who=(whoami); files=@{}; dir='?'; control='?'}",
            "foreach($f in $files){",
            "  try { $null=[IO.File]::ReadAllText($f); $r.files[$f]='READABLE' }",
            "  catch { $r.files[$f]='DENIED:'+$_.Exception.InnerException.GetType().Name }",
            "}",
            f"try {{ $null=[IO.Directory]::GetFiles('{secret_dir.as_posix()}'); $r.dir='LISTABLE' }}",
            "catch { $r.dir='DENIED:'+$_.Exception.InnerException.GetType().Name }",
            f"try {{ $null=[IO.File]::ReadAllText('{control.as_posix()}'); $r.control='READABLE' }}",
            "catch { $r.control='DENIED' }",
            "$r | ConvertTo-Json -Compress",
            "",
        ]),
    )
    _write_ps1(
        wrapper,
        "\n".join([
            "param([string]$UserName,[string]$Password,[string]$Probe,[string]$Out,[string]$Err)",
            "$sec=ConvertTo-SecureString $Password -AsPlainText -Force",
            "$cred=New-Object System.Management.Automation.PSCredential(\"$env:COMPUTERNAME\\$UserName\",$sec)",
            "$ps=\"$env:SystemRoot\\System32\\WindowsPowerShell\\v1.0\\powershell.exe\"",
            "$p=Start-Process -FilePath $ps -Credential $cred -Wait -PassThru "
            "-ArgumentList @('-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass','-File',$Probe) "
            "-RedirectStandardOutput $Out -RedirectStandardError $Err -WindowStyle Hidden",
            "exit $p.ExitCode",
            "",
        ]),
    )
    try:
        created = subprocess.run(
            ["net", "user", user, password, "/add", "/y"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        if created.returncode != 0:
            raise SystemExit(f"创建临时普通账户失败：{created.stderr or created.stdout}")
        for path in (out_file, err_file):
            path.unlink(missing_ok=True)
        result = subprocess.run(
            [
                str(Path(os.environ["SYSTEMROOT"]) / "System32" / "WindowsPowerShell" / "v1.0"
                    / "powershell.exe"),
                "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                "-File", str(wrapper),
                "-UserName", user, "-Password", password,
                "-Probe", str(probe_path), "-Out", str(out_file), "-Err", str(err_file),
            ],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
        )
        if result.returncode != 0:
            detail = _read_ps_redirect(err_file) if err_file.exists() else result.stderr
            raise SystemExit(f"普通账户探测进程失败：{detail}")
        probe = json.loads(_read_ps_redirect(out_file))
        return {"user": user, "probe": probe}
    finally:
        subprocess.run(
            ["net", "user", user, "/delete"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        shutil.rmtree(Path(os.environ["SYSTEMDRIVE"]) / "Users" / user, ignore_errors=True)
        for path in (probe_path, wrapper, out_file, err_file):
            path.unlink(missing_ok=True)


def write_evidence() -> None:
    lines = [
        "# 客户实例安全回归验收（Windows DACL / restore 边界 / 重启与停止身份 / 冷启动首请求）",
        "",
        f"- 时间：{datetime.now(UTC).isoformat()}",
        f"- 基准：{HEAD}（本轮修改前）",
        "- 本记录不含任何密钥/口令内容；只记录路径、SID、退出码与状态。",
        "",
        "## 步骤",
        "",
        "| 步骤 | 结果 | 细节 |",
        "|---|---|---|",
    ]
    for item in RECORDS:
        if "step" in item:
            mark = "✅" if item["ok"] else "❌"
            detail = str(item["detail"]).replace("|", "\\|")
            lines.append(f"| {item['step']} | {mark} | {detail} |")
    lines += ["", "## 命令（退出码）", "", "```text"]
    for item in RECORDS:
        if "cmd" in item:
            lines.append(f"$ {item['cmd']}  → exit={item['exit']}")
            for line in item.get("out", []):
                lines.append(f"  out: {line}")
            for line in item.get("err", []):
                lines.append(f"  err: {line}")
    lines += ["```", "", "## 剩余边界", "",
              "- Windows DACL 由创建实例的账户持有；换账户运行 serve 需先以目标账户重新 provision/restore。",
              "- restore 的失败清理只覆盖本工具创建的资源；外部手工创建的库/角色不在清理范围。",
              "- 冷启动回归覆盖 3 次重启 × 每轮首轮及后续请求，未做长稳压测。",
              ""]
    EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
    EVIDENCE.write_text("\n".join(lines), encoding="utf-8")


HEAD = "HEAD"


def main(argv: list[str] | None = None) -> int:
    global HEAD
    parser = argparse.ArgumentParser(description="客户实例安全回归验收")
    parser.add_argument(
        "--acl-probe-json",
        help="预先由 scripts/verify_secret_acl_windows.ps1 生成的普通账户探测结果"
             "（当 Python 进程不是提升令牌时使用）",
    )
    parser.add_argument(
        "--reuse", action="store_true",
        help="复用已存在且已初始化的 .acceptance/sec-a（不删除、不重新 provision）",
    )
    args = parser.parse_args(argv)

    HEAD = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True
    ).stdout.strip() or "unknown"

    if args.reuse:
        if not (A["workdir"] / "instance.json").exists():
            raise SystemExit(f"--reuse 需要已初始化的实例：{A['workdir']}/instance.json")
        instance_cmd("stop", "--workdir", str(A["workdir"]), expect_fail=True)
        cleanup_leftover_ports((FOREIGN_PORT, RESTORE_PORTS["db_port"],
                                RESTORE_PORTS["api_port"]))
    else:
        cleanup_leftover_ports((A["db_port"], A["api_port"], FOREIGN_PORT,
                                RESTORE_PORTS["db_port"], RESTORE_PORTS["api_port"]))
        if (A["workdir"] / "instance.json").exists():
            instance_cmd("stop", "--workdir", str(A["workdir"]), expect_fail=True)
        shutil.rmtree(A["workdir"], ignore_errors=True)
    for workdir in (BASE / "sec-restore-nonempty", BASE / "sec-restore-empty",
                    BASE / "sec-restore-fresh", BASE / "sec-restore-good"):
        if (workdir / "instance.json").exists():
            instance_cmd("stop", "--workdir", str(workdir), expect_fail=True)
        shutil.rmtree(workdir, ignore_errors=True)
    time.sleep(1)

    if not args.reuse:
        instance_cmd(
            "provision", "--workdir", str(A["workdir"]), "--db-port", str(A["db_port"]),
            "--api-port", str(A["api_port"]), "--db-name", A["db_name"], "--name", A["name"],
        )
    record = load(A["workdir"])
    instance_cmd("serve", "--workdir", str(A["workdir"]))
    token = mint(A["workdir"], record)

    # ---------- 1. Windows DACL + 普通账户实证 ----------
    desc_dir = winacl.describe(A["workdir"] / "secrets")
    desc_jwt = winacl.describe(A["workdir"] / "secrets" / "jwt.secret")
    step("secret-dir-acl-private", bool(desc_dir["protected"]) and len(desc_dir["aces"]) == 1,
         json.dumps(desc_dir, ensure_ascii=False))
    step("secret-file-acl-private", bool(desc_jwt["protected"]) and len(desc_jwt["aces"]) == 1,
         json.dumps(desc_jwt, ensure_ascii=False))

    if args.acl_probe_json:
        probe_path = Path(args.acl_probe_json).resolve()
        probe_bundle = json.loads(probe_path.read_text(encoding="utf-8-sig"))
        probe_user = str(probe_bundle.get("user", ""))
        probe_data = probe_bundle.get("probe", {})
        same_dir = (
            Path(str(probe_bundle.get("secret_dir", ""))).resolve()
            == (A["workdir"] / "secrets").resolve()
        )
        step("acl-probe-source", same_dir, f"{probe_path}（普通账户：{probe_user}）")
    else:
        try:
            probe_bundle = standard_user_probe(A["workdir"] / "secrets", ROOT / "README.md")
            probe_user = str(probe_bundle["user"])
            probe_data = probe_bundle["probe"]
            same_dir = True
        except SystemExit as exc:
            probe_data = {}
            probe_user = ""
            same_dir = False
            step(
                "acl-probe-source", False,
                f"{exc}；当前 Python 令牌不是提升令牌时，请先以管理员 PowerShell 运行 "
                "scripts/verify_secret_acl_windows.ps1，再用 --acl-probe-json 传入结果",
            )

    denied_all = bool(probe_data) and all(
        str(value).startswith("DENIED") for value in probe_data.get("files", {}).values()
    )
    step("ordinary-user-cannot-read-secrets",
         same_dir and denied_all and str(probe_data.get("dir", "")).startswith("DENIED")
         and probe_data.get("control") == "READABLE"
         and probe_data.get("admin_group") is False
         and probe_user.lower() in str(probe_data.get("who", "")).lower(),
         json.dumps(probe_data, ensure_ascii=False))

    # ---------- 2. restore 只接受全新目录；失败只清理本次资源 ----------
    dump = BASE / "sec-a.dump"
    instance_cmd("backup", "--workdir", str(A["workdir"]), "--out", str(dump))
    blob = dump.read_bytes()
    truncated = BASE / "sec-truncated.dump"
    truncated.write_bytes(blob[:-800])
    listed = run([str(pg_bin() / "pg_restore.exe"), "--list", str(truncated)],
                 expect_fail=True)
    step("truncated-dump-still-listable", listed.returncode == 0,
         f"pg_restore --list exit={listed.returncode}（用于制造“清单可读、恢复中途失败”）")

    nonempty = BASE / "sec-restore-nonempty"
    nonempty.mkdir(parents=True)
    sentinel = nonempty / "sentinel.txt"
    sentinel.write_text("keep-me", encoding="utf-8")
    refused = instance_cmd(
        "restore", "--from", str(dump), "--workdir", str(nonempty),
        "--db-port", str(RESTORE_PORTS["db_port"]), "--api-port", str(RESTORE_PORTS["api_port"]),
        "--db-name", "fkh_sec_r_nonempty", "--name", "r-nonempty", expect_fail=True,
    )
    step("restore-refuses-nonempty-target",
         refused.returncode != 0 and sentinel.read_text(encoding="utf-8") == "keep-me"
         and not (nonempty / "instance.json").exists(),
         f"exit={refused.returncode}；sentinel 保留")

    empty = BASE / "sec-restore-empty"
    empty.mkdir(parents=True)
    failed_empty = instance_cmd(
        "restore", "--from", str(truncated), "--workdir", str(empty),
        "--db-port", str(RESTORE_PORTS["db_port"]), "--api-port", str(RESTORE_PORTS["api_port"]),
        "--db-name", "fkh_sec_r_empty", "--name", "r-empty", expect_fail=True,
    )
    step("restore-failure-keeps-precreated-empty-dir",
         failed_empty.returncode != 0 and empty.exists()
         and list(empty.iterdir()) == [] and not port_listening(RESTORE_PORTS["api_port"]),
         f"exit={failed_empty.returncode}；目录仍在且为空")

    fresh = BASE / "sec-restore-fresh"
    failed_fresh = instance_cmd(
        "restore", "--from", str(truncated), "--workdir", str(fresh),
        "--db-port", str(RESTORE_PORTS["db_port"]), "--api-port", str(RESTORE_PORTS["api_port"]),
        "--db-name", "fkh_sec_r_fresh", "--name", "r-fresh", expect_fail=True,
    )
    step("restore-failure-removes-created-dir",
         failed_fresh.returncode != 0 and not fresh.exists()
         and not port_listening(RESTORE_PORTS["api_port"]),
         f"exit={failed_fresh.returncode}；目录已移除")

    good = BASE / "sec-restore-good"
    instance_cmd(
        "restore", "--from", str(dump), "--workdir", str(good),
        "--db-port", str(RESTORE_PORTS["db_port"]), "--api-port", str(RESTORE_PORTS["api_port"]),
        "--db-name", "fkh_sec_r_good", "--name", "r-good",
    )
    restored = load(good)
    good_acl = winacl.describe(good / "secrets" / "jwt.secret")
    instance_cmd("serve", "--workdir", str(good))
    step("restore-good-instance-serves",
         port_listening(int(restored["api_port"])) and good_acl["protected"],
         f"api={restored['api_port']}；jwt ACL protected={good_acl['protected']}")
    instance_cmd("stop", "--workdir", str(good))

    # ---------- 3a. 自定义域登记后仍能重启 ----------
    code, body = http(
        "POST", api(record, "/v1/config/domains"),
        payload={"code": "custom_field", "label": "客户自定义域"}, token=token,
    )
    step("custom-domain-registered", code in (201, 409), f"HTTP {code} {body}")
    instance_cmd("stop", "--workdir", str(A["workdir"]))
    restarted = instance_cmd("serve", "--workdir", str(A["workdir"]))
    code, body = http("GET", api(record, "/v1/config/domains"), token=token)
    codes = [item["code"] for item in body.get("domains", [])]
    step("restart-after-custom-domain",
         restarted.returncode == 0 and code == 200 and "custom_field" in codes,
         f"serve_exit={restarted.returncode}；domains={codes}")

    # ---------- 3b. pid 指向其他服务：拒绝停止，对方仍在运行 ----------
    foreign_app = BASE / "foreign_app.py"
    foreign_app.write_text(
        "\n".join([
            "async def app(scope, receive, send):",
            "    if scope['type'] == 'http':",
            "        await send({'type': 'http.response.start', 'status': 200, 'headers': []})",
            "        await send({'type': 'http.response.body', 'body': b'foreign'})",
            "",
        ]),
        encoding="utf-8",
    )
    log_path = BASE / "foreign.log"
    with log_path.open("w", encoding="utf-8") as log:
        foreign = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "foreign_app:app", "--host", "127.0.0.1",
             "--port", str(FOREIGN_PORT)],
            cwd=BASE, stdout=log, stderr=subprocess.STDOUT,
        )
    pid_file = A["workdir"] / "api.pid"
    original_pid = pid_file.read_text(encoding="utf-8")
    try:
        deadline = time.time() + 20
        while time.time() < deadline and not port_listening(FOREIGN_PORT):
            time.sleep(0.2)
        if not port_listening(FOREIGN_PORT):
            raise SystemExit("外部服务未启动")
        pid_file.write_text(str(foreign.pid), encoding="utf-8")
        refused_foreign = instance_cmd(
            "stop", "--workdir", str(A["workdir"]), expect_fail=True
        )
        time.sleep(0.5)
        step("stop-refuses-foreign-pid-and-keeps-it",
             refused_foreign.returncode != 0 and foreign.poll() is None
             and port_listening(FOREIGN_PORT) and port_listening(int(record["api_port"])),
             f"exit={refused_foreign.returncode}；foreign_alive={foreign.poll() is None}；"
             f"foreign_port={port_listening(FOREIGN_PORT)}")
    finally:
        pid_file.write_text(original_pid, encoding="utf-8")
        try:
            for child in psutil.Process(foreign.pid).children(recursive=True):
                child.terminate()
            foreign.terminate()
            foreign.wait(timeout=10)
        except Exception:
            pass

    # ---------- 4. 冷启动后首个 review 必须 200（不重试） ----------
    cold_runs: list[dict[str, Any]] = []
    for cycle in range(3):
        instance_cmd("stop", "--workdir", str(A["workdir"]))
        instance_cmd("serve", "--workdir", str(A["workdir"]))
        for index in range(8):
            key = f"cold-{RUN_TAG}-{cycle}-{index}"
            started = time.perf_counter()
            _, upload_code, review_code = upload(
                record, token, title=f"冷启动回归 {key}",
                text=f"# {key}\n\n冷启动审核首请求回归正文。\n",
                domain="custom_field", key=key,
            )
            cold_runs.append({
                "cycle": cycle, "index": index, "upload": upload_code,
                "review": review_code, "ms": round((time.perf_counter() - started) * 1000, 1),
            })
    bad = [item for item in cold_runs if item["review"] != 200]
    step("cold-start-first-review-passes-without-retry", not bad,
         f"{len(cold_runs)} 次上传+立即审核，非 200={len(bad)}；"
         f"首轮={[item['review'] for item in cold_runs if item['index'] == 0]}")

    instance_cmd("stop", "--workdir", str(A["workdir"]))
    write_evidence()
    return 0 if all(item["ok"] for item in RECORDS if "step" in item) else 1


if __name__ == "__main__":
    raise SystemExit(main())
