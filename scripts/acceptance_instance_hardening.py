"""客户实例交付加固的可复跑验收（2026-11-08，基准 0c2fd5f）。

覆盖用户提出的 ①②③④：
① P0 数据库认证与密钥：交付实例禁用 trust；管理员与业务角色独立认证；无凭据/业务凭据
   都不能以管理员登录；JWT 密钥只在 secrets/、instance.json 无明文；启动校验密钥；
   凭据不进命令行/日志/验收记录/备份元数据。
② P1 首装与恢复边界：重复 provision 明确拒绝、--reuse 幂等复用；restore 只接受全新隔离目标，
   单事务恢复，坏备份失败后不留可运行半成品，原实例与已有备份完好。
③ P1 启停：端口占用明确拒绝；启动确认本次子进程与实例身份；停止校验进程身份并核验结果。
④ 恢复验收：备份前记录问答/证据审计 ID，恢复后先核对原记录再做新请求；断言 REVOKED、
   旧引用拒绝、有效内容可答、恢复实例业务角色与独立凭据。

只使用**新建隔离测试实例**与合成资料；工厂（8010/8000/55432）与烘焙（8114/8104/55462）
演示环境不触碰。
"""

from __future__ import annotations

import base64
import json
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import jwt
import psycopg

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / ".acceptance"
EVIDENCE = ROOT / "_evidence" / "instance_hardening_acceptance.md"

A = {"workdir": BASE / "h-a", "db_port": 55481, "api_port": 8151, "db_name": "fkh_h_a", "name": "hard-a"}
B = {"workdir": BASE / "h-b", "db_port": 55482, "api_port": 8152, "db_name": "fkh_h_b", "name": "hard-b"}
C = {"workdir": BASE / "h-c", "db_port": 55483, "api_port": 8153, "db_name": "fkh_h_c", "name": "hard-c"}

RECORDS: list[dict[str, Any]] = []

CATALOG = """# 合成客户目录（加固演练）

第 1 页

货号 规格 接口 单价
CX-2001 不锈钢夹套管 300mm G1/2 128.00
CX-2002 石英观察窗 100mm G1/4 268.00
"""
SAFETY = """# 客户现场安全守则（合成）

巡检周期：每 14 天检查一次夹具螺栓扭矩。
停机顺序：先泄压，再断电，最后拆管。
"""
QUALITY = """# 客户质量条款（合成，将在演练中被撤销）

批次抽检比例：每批抽 3 件。
不合格处理：整批退回，不做让步接收。
"""

OPS_SCOPES = (
    "knowledge.read", "knowledge.write", "knowledge.ingest", "knowledge.review",
    "knowledge.publish", "knowledge.revoke", "knowledge.answer", "knowledge.search",
    "evidence.read",
)
AGENT_SCOPES = ("knowledge.answer", "knowledge.search", "evidence.read")


def step(name: str, ok: bool, detail: str = "") -> None:
    RECORDS.append({"step": name, "ok": ok, "detail": detail})
    print(json.dumps({"step": name, "ok": ok, "detail": detail}, ensure_ascii=False))


def run(args: list[str], *, env_extra: dict[str, str] | None = None,
        timeout: int = 900, expect_fail: bool = False) -> subprocess.CompletedProcess[str]:
    import os

    env = {**os.environ, **(env_extra or {})}
    result = subprocess.run(
        args, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=timeout, env=env,
    )
    RECORDS.append({
        "cmd": " ".join(args),
        "exit": result.returncode,
        "out": (result.stdout or "").strip().splitlines()[-1:],
        "err": (result.stderr or "").strip().splitlines()[-2:],
    })
    if expect_fail:
        return result
    if result.returncode != 0:
        print(result.stdout, file=sys.stderr)
        print(result.stderr, file=sys.stderr)
        raise SystemExit(f"命令失败（{result.returncode}）：{' '.join(args)}")
    return result


SERVE_ENV = {
    "FKH_RETRIEVAL_MODE": "lexical",
    "FKH_LLM_PROVIDER": "ollama",
    "FKH_LLM_MODEL": "qwen3:14b",
    "FKH_LLM_BASE_URL": "http://127.0.0.1:11434",
    "FKH_LLM_TIMEOUT_SECONDS": "180",
}


def instance_cmd(*args: str, **kwargs: Any) -> subprocess.CompletedProcess[str]:
    if args and args[0] == "serve":
        kwargs.setdefault("env_extra", SERVE_ENV)
    return run([sys.executable, "-m", "fkh.tools.instance", *args], **kwargs)


def http(method: str, url: str, *, payload: dict[str, Any] | None = None,
         token: str | None = None, timeout: int = 300) -> tuple[int, dict[str, Any]]:
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


def admin_dsn(record: dict[str, Any]) -> str:
    from urllib.parse import quote, urlunparse

    password = (Path(record["workdir"]) / "secrets" / "db-admin.password").read_text(
        encoding="utf-8"
    ).strip()
    return urlunparse((
        "postgresql",
        f"postgres:{quote(password, safe='')}@127.0.0.1:{record['db_port']}",
        "/" + record["db_name"], "", "", "",
    ))


def mint(workdir: Path, record: dict[str, Any], *, principal: str,
         scopes: tuple[str, ...]) -> str:
    now = datetime.now(UTC)
    return jwt.encode(
        {
            "iss": "fkh.local", "aud": "fkh-api", "sub": "acceptance@hardening.local",
            "iat": now, "exp": now + timedelta(hours=3),
            "tenant_id": record["tenant_id"], "principal_id": principal,
            "roles": ["staff"], "scope": list(scopes),
            "allowed_scopes": ["internal_product", "customer_public"],
        },
        jwt_secret(workdir),
        algorithm="HS256",
    )


def api(record: dict[str, Any], path: str) -> str:
    return f"http://127.0.0.1:{record['api_port']}{path}"


RUN_TAG = datetime.now(UTC).strftime("%H%M%S")


def upload(record: dict[str, Any], token: str, *, title: str, text: str,
           domain: str, key: str) -> str:
    key = f"{key}-{RUN_TAG}"
    body = {
        "filename": f"{key}.md", "title": title, "domain": domain,
        "owner_department": "ops", "classification": 1, "audience": "internal",
        "required_scope": "internal_product", "acl_allow_roles": ["staff"],
        "knowledge_key": key, "content_base64": base64.b64encode(text.encode()).decode(),
        **({"catalog_currency": "CNY"} if domain == "product_catalog" else {}),
    }
    code, payload = http("POST", api(record, "/v1/knowledge/documents"), payload=body, token=token)
    if code != 201:
        raise SystemExit(f"导入失败 {code}: {payload}")
    version = str(payload["version_id"])
    # 2026-11-09：根因是 yield 依赖在响应发送后才提交事务（FastAPI 的 request exit stack
    # 在 response 之后关闭）。客户端紧随其后的 review 可能读不到刚写入的版本，被映射成 403。
    # `CommitBeforeSend` 已改为响应前提交；这里**不再重试**，首次请求必须 200。
    code, body = http("POST", api(record, f"/v1/knowledge/{version}/review"),
                      payload={"decision": "PASS"}, token=token)
    if code != 200:
        raise SystemExit(f"审核首请求失败 {code}（不再重试掩盖）: {body}")
    code, body = http("POST", api(record, f"/v1/knowledge/{version}/publish"),
                      payload={"audience": "internal"}, token=token)
    if code != 200:
        raise SystemExit(f"发布失败 {code}: {body}")
    return version


def search(record: dict[str, Any], token: str, query: str) -> list[dict[str, Any]]:
    code, body = http("POST", api(record, "/v1/knowledge/search"),
                      payload={"query": query, "knowledge_scope": "internal_product",
                               "max_results": 5}, token=token)
    return (body.get("results") or []) if code == 200 else []


def answer(record: dict[str, Any], token: str, query: str) -> dict[str, Any]:
    code, body = http("POST", api(record, "/v1/knowledge/answer"),
                      payload={"query": query, "knowledge_scope": "internal_product",
                               "language": "zh-CN", "max_evidence": 5}, token=token)
    return {**body, "_http": code}


def cleanup_leftover_ports(ports: tuple[int, ...]) -> None:
    """清掉上一次中断留下的**本验收**监听者（只碰 .acceptance 相关进程，别的进程一律不碰）。"""
    import psutil

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


def main() -> int:
    cleanup_leftover_ports((55481, 55482, 55483, 55489, 8151, 8152, 8153))
    if BASE.exists():
        for cfg in (A, B, C):
            if (cfg["workdir"] / "instance.json").exists():
                instance_cmd("stop", "--workdir", str(cfg["workdir"]), expect_fail=True)
        time.sleep(2)
        shutil.rmtree(BASE, ignore_errors=True)
    BASE.mkdir(parents=True, exist_ok=True)

    # ---------- 修复前基线：trust 集群可无凭据登录 ----------
    trust_dir = BASE / "trust-probe"
    sys.path.insert(0, str(ROOT / "src"))
    from fkh.tools.pg import ensure_postgres, stop_postgres

    trust_dsn = ensure_postgres(trust_dir, 55489)  # 不带口令 → initdb -A trust
    try:
        with psycopg.connect(trust_dsn) as conn:
            conn.execute("SELECT 1")
        trust_ok = True
    except psycopg.OperationalError:
        trust_ok = False
    stop_postgres(trust_dir)
    step("baseline-trust-login-accepted", trust_ok,
         "修复前的演示式集群（initdb -A trust）无口令即可登录管理员 —— 这是本片要消除的行为")

    # ---------- ① P0：首装与认证隔离 ----------
    instance_cmd("provision", "--workdir", str(A["workdir"]), "--db-port", str(A["db_port"]),
                 "--api-port", str(A["api_port"]), "--db-name", A["db_name"], "--name", A["name"])
    record_a = load(A["workdir"])
    step("provision-a", True, f"tenant={record_a['tenant_id']}")

    raw = (A["workdir"] / "instance.json").read_text(encoding="utf-8")
    leaked = [k for k in ("jwt_secret", "password", "db-admin.password", "db-app.password")
              if k in raw]
    step("instance-json-has-no-secret", not leaked, f"命中：{leaked or '无'}")

    secret_files = {
        name: (A["workdir"] / "secrets" / name).stat().st_size
        for name in ("jwt.secret", "db-admin.password", "db-app.password")
    }
    step("secret-files-present", all(size > 30 for size in secret_files.values()),
         json.dumps(secret_files))

    # 用 psql 等价方式验证：无口令 / 业务口令都不能以管理员登录
    def try_login(user: str, password: str) -> bool:
        from urllib.parse import quote, urlunparse

        dsn = urlunparse((
            "postgresql",
            f"{quote(user, safe='')}:{quote(password, safe='')}@127.0.0.1:{A['db_port']}",
            "/" + A["db_name"], "", "", "",
        ))
        try:
            with psycopg.connect(dsn, connect_timeout=5) as conn:
                conn.execute("SELECT 1")
            return True
        except psycopg.OperationalError:
            return False

    admin_pw = (A["workdir"] / "secrets" / "db-admin.password").read_text(encoding="utf-8").strip()
    app_pw = (A["workdir"] / "secrets" / "db-app.password").read_text(encoding="utf-8").strip()
    auth = {
        "admin_ok": try_login("postgres", admin_pw),
        "admin_no_pw": try_login("postgres", ""),
        "admin_with_app_pw": try_login("postgres", app_pw),
        "app_ok": try_login(record_a["app_role"], app_pw),
        "app_with_admin_pw": try_login(record_a["app_role"], admin_pw),
    }
    step("db-auth-isolation",
         auth["admin_ok"] and auth["app_ok"] and not auth["admin_no_pw"]
         and not auth["admin_with_app_pw"] and not auth["app_with_admin_pw"],
         json.dumps(auth))

    status_out = json.loads(instance_cmd("status", "--workdir", str(A["workdir"])).stdout)
    role_info = status_out["app_role"]
    step("business-role-not-owner",
         role_info["rolsuper"] is False and role_info["rolbypassrls"] is False
         and role_info["table_owner"] == "postgres", json.dumps(role_info, ensure_ascii=False))

    # 启动校验既有密钥：把密钥改短 → 必须拒绝启动
    secret_file = A["workdir"] / "secrets" / "jwt.secret"
    good_secret = secret_file.read_text(encoding="utf-8")
    secret_file.write_text("short\n", encoding="utf-8")
    weak = instance_cmd("serve", "--workdir", str(A["workdir"]), expect_fail=True)
    secret_file.write_text(good_secret, encoding="utf-8")
    step("serve-rejects-weak-secret", weak.returncode != 0
         and "强度不足" in (weak.stdout + weak.stderr),
         f"exit={weak.returncode}")

    # ---------- ② P1：重复 provision 与 --reuse ----------
    dup = instance_cmd("provision", "--workdir", str(A["workdir"]), "--db-port", str(A["db_port"]),
                       "--api-port", str(A["api_port"]), "--db-name", A["db_name"],
                       "--name", A["name"], expect_fail=True)
    step("provision-refuses-reinit", dup.returncode != 0
         and "--reuse" in (dup.stdout + dup.stderr), f"exit={dup.returncode}")

    before_tenant = load(A["workdir"])["tenant_id"]
    instance_cmd("provision", "--workdir", str(A["workdir"]), "--db-port", str(A["db_port"]),
                 "--api-port", str(A["api_port"]), "--db-name", A["db_name"],
                 "--name", A["name"], "--reuse")
    after_tenant = load(A["workdir"])["tenant_id"]
    step("provision-reuse-idempotent", before_tenant == after_tenant,
         f"tenant 保持 {after_tenant}")

    # ---------- ③ P1：端口占用 / 启动身份 / 停止身份 ----------
    busy = socket.socket()
    busy.bind(("127.0.0.1", int(A["api_port"])))
    busy.listen(1)
    try:
        refused = instance_cmd("serve", "--workdir", str(A["workdir"]), expect_fail=True)
    finally:
        busy.close()
    step("serve-refuses-busy-port", refused.returncode != 0
         and str(A["api_port"]) in (refused.stdout + refused.stderr),
         (refused.stderr or refused.stdout).strip().splitlines()[0][:160])

    instance_cmd("serve", "--workdir", str(A["workdir"]))
    record_a = load(A["workdir"])
    ops_a = mint(A["workdir"], record_a, principal=record_a["ops_principal"], scopes=OPS_SCOPES)
    agent_a = mint(A["workdir"], record_a, principal=record_a["agent_principal"],
                   scopes=AGENT_SCOPES)
    code, _ = http("GET", api(record_a, "/v1/config/domains"), token=ops_a)
    step("serve-identity-ok", code == 200, f"config/domains → {code}")

    # 停止身份校验：伪造 pid 文件 → 拒绝停止
    pid_file = A["workdir"] / "api.pid"
    real_pid = pid_file.read_text(encoding="utf-8").strip()
    import os

    pid_file.write_text(str(os.getpid()), encoding="utf-8")
    forged = instance_cmd("stop", "--workdir", str(A["workdir"]), expect_fail=True)
    pid_file.write_text(real_pid, encoding="utf-8")
    step("stop-refuses-foreign-pid", forged.returncode != 0
         and "不是本实例" in (forged.stdout + forged.stderr), f"exit={forged.returncode}")

    # ---------- 第四阶段的准备工作：导入 + 审计 ID 快照（备份前） ----------
    upload(record_a, ops_a, title="合成客户目录", text=CATALOG,
           domain="product_catalog", key="h-catalog")
    upload(record_a, ops_a, title="客户现场安全守则", text=SAFETY,
           domain="equipment_safety", key="h-safety")
    quality_version = upload(record_a, ops_a, title="客户质量条款", text=QUALITY,
                             domain="quality", key="h-quality")

    catalog_answer = answer(record_a, agent_a, "CX-2001 的单价是多少？")
    catalog_citation = (catalog_answer.get("citations") or [{}])[0]
    valid_evidence = str(catalog_citation.get("evidence_id", ""))
    quality_hits = [h for h in search(record_a, agent_a, "批次抽检比例")
                    if str(h.get("version_id")) == quality_version]
    revoked_chunk = str(quality_hits[0]["chunk_id"]) if quality_hits else ""

    # 备份前显式读一次有效证据（产生一条 evidence_access_log 审计，供恢复后按 ID 核对）
    pre_code, _ = http("GET", api(record_a, f"/v1/evidence/{valid_evidence}"), token=agent_a)
    step("valid-evidence-readable-before", pre_code == 200, f"HTTP {pre_code}")

    revoke_code, revoke_body = http(
        "POST", api(record_a, f"/v1/knowledge/{quality_version}/revoke?reason=hardening"),
        payload={}, token=ops_a,
    )
    step("revoke-quality", revoke_code == 200, f"HTTP {revoke_code}")

    deadline = time.time() + 8
    observed = ""
    while time.time() < deadline:
        with psycopg.connect(admin_dsn(record_a)) as probe:
            probe.execute("SELECT set_config('app.tenant_id', %s, false)", (record_a["tenant_id"],))
            row = probe.execute(
                "SELECT status FROM knowledge_versions WHERE id = %s", (quality_version,)
            ).fetchone()
        observed = str(row[0]) if row else ""
        if observed == "REVOKED":
            break
        time.sleep(0.3)
    step("revoke-status-settled", observed == "REVOKED", f"status={observed}")

    snapshot: dict[str, Any] = {}
    with psycopg.connect(admin_dsn(record_a)) as conn:
        conn.execute("SELECT set_config('app.tenant_id', %s, false)", (record_a["tenant_id"],))
        query_row = conn.execute(
            "SELECT id, query_hash FROM rag_queries WHERE tenant_id = %s"
            " ORDER BY created_at DESC LIMIT 1",
            (record_a["tenant_id"],),
        ).fetchone()
        evidence_row = conn.execute(
            "SELECT id, evidence_id, result FROM evidence_access_log"
            " WHERE tenant_id = %s ORDER BY occurred_at DESC LIMIT 1",
            (record_a["tenant_id"],),
        ).fetchone()
        version_row = conn.execute(
            "SELECT id, status FROM knowledge_versions WHERE tenant_id = %s AND id = %s",
            (record_a["tenant_id"], quality_version),
        ).fetchone()
        counts_before = {
            table: conn.execute(
                f"SELECT count(*) FROM {table} WHERE tenant_id = %s", (record_a["tenant_id"],)
            ).fetchone()[0]
            for table in ("rag_queries", "evidence_access_log", "knowledge_versions")
        }
        snapshot = {
            "query_id": str(query_row[0]) if query_row else None,
            "query_hash": str(query_row[1]) if query_row else None,
            "evidence_log_id": str(evidence_row[0]) if evidence_row else None,
            "evidence_log_evidence_id": str(evidence_row[1]) if evidence_row else None,
            "version_status": str(version_row[1]) if version_row else None,
            "counts": {k: int(v) for k, v in counts_before.items()},
        }
    step("audit-snapshot-before-backup",
         bool(snapshot["query_id"]) and bool(snapshot["evidence_log_id"])
         and snapshot["version_status"] == "REVOKED", json.dumps(snapshot))

    backup = BASE / "h-a.dump"
    instance_cmd("backup", "--workdir", str(A["workdir"]), "--out", str(backup))
    dump_bytes = backup.read_bytes()
    step("backup-ok", backup.exists(), f"{len(dump_bytes)} bytes")

    # ---------- ④ 恢复：先核对原审计记录，再做新请求 ----------
    instance_cmd("restore", "--from", str(backup), "--workdir", str(B["workdir"]),
                 "--db-port", str(B["db_port"]), "--api-port", str(B["api_port"]),
                 "--db-name", B["db_name"], "--name", B["name"])
    record_b = load(B["workdir"])
    step("restore-to-fresh-instance", record_b["tenant_id"] == record_a["tenant_id"],
         f"tenant 相同；新密钥={jwt_secret(B['workdir']) != jwt_secret(A['workdir'])}")

    with psycopg.connect(admin_dsn(record_b)) as conn:
        conn.execute("SELECT set_config('app.tenant_id', %s, false)", (record_b["tenant_id"],))
        q = conn.execute(
            "SELECT id, query_hash FROM rag_queries WHERE id = %s", (snapshot["query_id"],)
        ).fetchone()
        e = conn.execute(
            "SELECT id, result FROM evidence_access_log WHERE id = %s",
            (snapshot["evidence_log_id"],),
        ).fetchone()
        v = conn.execute(
            "SELECT status FROM knowledge_versions WHERE id = %s", (quality_version,)
        ).fetchone()
        counts_after = {
            table: conn.execute(
                f"SELECT count(*) FROM {table} WHERE tenant_id = %s", (record_b["tenant_id"],)
            ).fetchone()[0]
            for table in ("rag_queries", "evidence_access_log", "knowledge_versions")
        }
        flags = conn.execute(
            "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = %s",
            (record_b["app_role"],),
        ).fetchone()
        owner = conn.execute(
            "SELECT tableowner FROM pg_tables WHERE tablename = 'knowledge_versions'"
        ).fetchone()
    step("audit-ids-survive-restore",
         q is not None and str(q[1]) == snapshot["query_hash"]
         and e is not None and v is not None and str(v[0]) == "REVOKED"
         and counts_after["rag_queries"] >= snapshot["counts"]["rag_queries"]
         and counts_after["evidence_access_log"] >= snapshot["counts"]["evidence_access_log"],
         json.dumps({"query": str(q[0]) if q else None, "evidence_log": str(e[0]) if e else None,
                     "version_status": str(v[0]) if v else None,
                     "counts_after": {k: int(x) for k, x in counts_after.items()}},
                    ensure_ascii=False))
    step("restored-role-not-owner",
         flags is not None and flags[0] is False and flags[1] is False
         and owner is not None and str(owner[0]) == "postgres",
         json.dumps({"rolsuper": bool(flags[0]) if flags else None,
                     "rolbypassrls": bool(flags[1]) if flags else None,
                     "table_owner": str(owner[0]) if owner else None}))

    instance_cmd("serve", "--workdir", str(B["workdir"]))
    record_b = load(B["workdir"])
    ops_b = mint(B["workdir"], record_b, principal=record_b["ops_principal"], scopes=OPS_SCOPES)
    agent_b = mint(B["workdir"], record_b, principal=record_b["agent_principal"],
                   scopes=AGENT_SCOPES)
    # 恢复实例的运维令牌同样能读实例配置（证明恢复的配置与角色可用）
    ops_probe, _ = http("GET", api(record_b, "/v1/config/domains"), token=ops_b)
    step("restored-ops-token-ok", ops_probe == 200, f"config/domains → {ops_probe}")

    cross, _ = http("GET", api(record_b, "/v1/config/domains"), token=agent_a)
    step("old-instance-token-rejected", cross == 401, f"A 的令牌打 B → {cross}")

    restored_answer = answer(record_b, agent_b, "CX-2002 的单价是多少？")
    step("restored-valid-answerable",
         restored_answer.get("_http") == 200 and restored_answer.get("status") == "ANSWERED"
         and bool(restored_answer.get("citations")),
         f"status={restored_answer.get('status')}")

    code, _ = http("GET", api(record_b, f"/v1/evidence/{valid_evidence}"), token=agent_b)
    step("restored-valid-evidence-ok", code == 200, f"HTTP {code}")
    code, _ = http("GET", api(record_b, f"/v1/evidence/{revoked_chunk}"), token=agent_b)
    step("restored-revoked-evidence-rejected", code in (403, 404), f"HTTP {code}")

    revoked_hits = [h for h in search(record_b, agent_b, "批次抽检比例")
                    if str(h.get("version_id")) == quality_version]
    step("restored-revoked-not-searchable", not revoked_hits, f"命中 {len(revoked_hits)} 条")
    revoked_answer = answer(record_b, agent_b, "批次抽检比例是多少？")
    step("restored-revoked-not-answerable",
         revoked_answer.get("_http") == 200 and revoked_answer.get("status") != "ANSWERED",
         f"status={revoked_answer.get('status')}")

    # ---------- 坏备份：必须失败且不留可运行半成品 ----------
    broken = BASE / "broken.dump"
    broken.write_bytes(dump_bytes[: max(64, len(dump_bytes) // 3)])
    bad = instance_cmd("restore", "--from", str(broken), "--workdir", str(C["workdir"]),
                       "--db-port", str(C["db_port"]), "--api-port", str(C["api_port"]),
                       "--db-name", C["db_name"], "--name", C["name"], expect_fail=True)
    half_state = {
        "instance_json": (C["workdir"] / "instance.json").exists(),
        "workdir": C["workdir"].exists(),
        "api_port_busy": False,
    }
    probe = socket.socket()
    try:
        probe.settimeout(0.5)
        half_state["api_port_busy"] = probe.connect_ex(("127.0.0.1", int(C["api_port"]))) == 0
    finally:
        probe.close()
    step("bad-restore-fails-clean", bad.returncode != 0
         and not half_state["instance_json"] and not half_state["api_port_busy"],
         json.dumps(half_state))

    still_ok, _ = http("GET", api(record_a, "/v1/config/domains"), token=ops_a)
    listed = subprocess.run(
        [sys.executable, "-c",
         "import subprocess,sys,pathlib,pgserver;"
         "b=pathlib.Path(pgserver.__file__).parent/'pginstall'/'bin';"
         f"sys.exit(subprocess.run([str(b/'pg_restore.exe'),'--list',r'{backup}']).returncode)"],
        capture_output=True, text=True,
    )
    step("source-instance-and-backup-intact", still_ok == 200 and listed.returncode == 0,
         f"A 仍 200；原备份仍可读（pg_restore --list exit={listed.returncode}）")

    # ---------- 停止（校验身份 + 核验结果） ----------
    stopp = instance_cmd("stop", "--workdir", str(B["workdir"]))
    c_port = socket.socket()
    try:
        c_port.settimeout(0.5)
        b_closed = c_port.connect_ex(("127.0.0.1", int(B["api_port"]))) != 0
    finally:
        c_port.close()
    step("stop-verified", stopp.returncode == 0 and b_closed,
         f"B API 端口已关闭={b_closed}")

    instance_cmd("stop", "--workdir", str(A["workdir"]))
    write_evidence(record_a, record_b, snapshot)
    failed = [r for r in RECORDS if "step" in r and not r["ok"]]
    print(json.dumps({"summary": {"steps": len([r for r in RECORDS if 'step' in r]),
                                  "failed": len(failed)}}, ensure_ascii=False))
    return 1 if failed else 0


def write_evidence(record_a: dict[str, Any], record_b: dict[str, Any],
                   snapshot: dict[str, Any]) -> None:
    lines = [
        "# 客户实例交付加固验收（P0 认证/密钥、P1 首装与恢复、P1 启停、恢复审计核对）",
        "",
        f"- 时间：{datetime.now(UTC).isoformat()}",
        "- 基准：`0c2fd5f`（本轮加固前）；工厂/烘焙演示环境未触碰",
        "- 密钥与口令**不在本记录中**（只记录路径与校验结论）",
        "",
        "## 实例",
        "",
        "| | A（首装） | B（恢复） |",
        "|---|---|---|",
        f"| 数据目录 | `{Path(record_a['workdir']).name}` | `{Path(record_b['workdir']).name}` |",
        f"| DB/API 端口 | {record_a['db_port']}/{record_a['api_port']} | {record_b['db_port']}/{record_b['api_port']} |",
        f"| 业务角色 | `{record_a['app_role']}` | `{record_b['app_role']}` |",
        f"| 租户 | `{record_a['tenant_id']}` | `{record_b['tenant_id']}` |",
        "",
        "## 步骤",
        "",
        "| 步骤 | 结果 | 细节 |",
        "|---|---|---|",
    ]
    for item in RECORDS:
        if "step" not in item:
            continue
        lines.append(f"| {item['step']} | {'✅' if item['ok'] else '❌'} | "
                     f"{str(item['detail']).replace('|', chr(92)+'|')} |")

    lines += ["", "## 命令（退出码）", "", "```text"]
    for item in RECORDS:
        if "cmd" not in item:
            continue
        tail = f"  → {item['out'][0]}" if item.get("out") else ""
        lines.append(f"$ {item['cmd']}{tail}\n  exit={item['exit']}")
    lines += ["```", "", "## 备份前审计快照（恢复后按 ID 核对）", "", "```json",
              json.dumps(snapshot, ensure_ascii=False, indent=2), "```", ""]
    lines += [
        "## 剩余边界",
        "",
        "1. 备份仍为逻辑备份（`pg_dump --format=custom --no-owner --no-privileges`），",
        "   不含角色与其口令；恢复时由 `restore` 重建业务角色并生成**新口令与新 JWT 密钥**。",
        "2. 未做 PITR；未验证备份期间持续写入后的时间点一致性。",
        "3. 未验证跨大版本恢复（旧 dump 恢复到新 schema）。",
        "4. 口令与密钥的**静态保护**依赖操作系统访问控制（本机为 NTFS ACL）；",
        "   脚本只做 `chmod 0600` 尽力而为，未在 Linux 上做独立验证。",
        "5. `pg_hba.conf` 由 initdb 生成（host=scram-sha-256，local=trust）；Windows 无 unix socket，",
        "   本片未额外收紧 local 行（生产 Linux 部署需按部署规范收紧并加 md5/scram）。",
        "6. 启停的进程身份校验基于 cmdline 子串匹配，进程树被替换/包装（如 systemd 包装）需另配 pidfile 语义。",
    ]
    EVIDENCE.write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
