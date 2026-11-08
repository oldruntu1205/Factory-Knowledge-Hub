"""空环境首装 → 重启 → 备份恢复 的可复跑验收（2026-11-08）。

产品边界：一客户一套独立私域实例，共用 RAG 基座与 Knowledge API（ADR-009）。
本脚本只创建**临时合成实例**，不触碰正在运行的工厂（8010/8000/55432）与烘焙（8114/8104/55462）演示环境。

覆盖：
① 空数据目录 + 独立端口首装；只导入合成资料；迁移用管理员、API 用非 owner 业务角色；
   实例密钥重启后稳定；Agent 只给问答/检索/证据读取。
② 真实 HTTP：导入 → 审核 → 发布 → Agent 问答带引用 → 旧引用可定位；重启同实例后
   数据/权限/配置/有效令牌仍按预期工作。
③ 撤销一份已发布资料 → 备份 → 恢复到全新隔离实例（新目录/新端口/**新密钥**）→
   用恢复实例凭据验证：有效资料仍可答、已撤销资料与旧引用仍拒绝、审计记录保留。

产出：`_evidence/instance_lifecycle_acceptance.md`（已脱敏：不含口令与密钥）。
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import jwt

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / ".acceptance"
EVIDENCE = ROOT / "_evidence" / "instance_lifecycle_acceptance.md"

A = {
    "workdir": BASE / "instance-a",
    "db_port": 55471,
    "api_port": 8141,
    "db_name": "fkh_acc_a",
    "name": "customer-a",
}
B = {
    "workdir": BASE / "instance-b",
    "db_port": 55472,
    "api_port": 8142,
    "db_name": "fkh_acc_b",
    "name": "customer-b-restored",
}

RECORDS: list[dict[str, Any]] = []

CATALOG = """# 合成客户目录（演练用，与工厂/烘焙演示无关）

第 1 页

货号 规格 接口 单价
CX-1001 不锈钢夹套管 300mm G1/2 128.00
CX-1002 石英观察窗 100mm G1/4 268.00
"""

SAFETY = """# 客户现场安全守则（合成）

巡检周期：每 14 天检查一次夹具螺栓扭矩。
停机顺序：先泄压，再断电，最后拆管。
"""

QUALITY = """# 客户质量条款（合成，将在演练中被撤销）

批次抽检比例：每批抽 3 件。
不合格处理：整批退回，不做让步接收。
"""


def step(name: str, ok: bool, detail: str = "") -> None:
    RECORDS.append({"step": name, "ok": ok, "detail": detail})
    print(json.dumps({"step": name, "ok": ok, "detail": detail}, ensure_ascii=False))


def run(
    args: list[str], *, timeout: int = 900, env_extra: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    env = None
    if env_extra:
        env = {**os.environ, **env_extra}
    result = subprocess.run(
        args, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=timeout, env=env,
    )
    RECORDS.append(
        {
            "cmd": " ".join(args),
            "exit": result.returncode,
            "out": (result.stdout or "").strip().splitlines()[-1:] if result.stdout else [],
        }
    )
    if result.returncode != 0:
        print(result.stdout, file=sys.stderr)
        print(result.stderr, file=sys.stderr)
        raise SystemExit(f"命令失败（{result.returncode}）：{' '.join(args)}")
    return result


def http(
    method: str, url: str, *, payload: dict[str, Any] | None = None, token: str | None = None,
    timeout: int = 300,
) -> tuple[int, dict[str, Any]]:
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"Content-Type": "application/json"} if data is not None else {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            body = resp.read().decode() or "{}"
            return resp.status, json.loads(body)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode() or "{}"
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            parsed = {"raw": body[:200]}
        return exc.code, parsed


def mint(secret: str, record: dict[str, Any], *, principal: str,
         scopes: tuple[str, ...], roles: tuple[str, ...] = ("staff",),
         allowed: tuple[str, ...] = ("internal_product", "customer_public")) -> str:
    now = datetime.now(UTC)
    return jwt.encode(
        {
            "iss": "fkh.local", "aud": "fkh-api", "sub": "acceptance@customer.local",
            "iat": now, "exp": now + timedelta(hours=2),
            "tenant_id": record["tenant_id"], "principal_id": principal,
            "roles": list(roles), "scope": list(scopes), "allowed_scopes": list(allowed),
        },
        secret,
        algorithm="HS256",
    )


OPS_SCOPES = (
    "knowledge.read", "knowledge.write", "knowledge.ingest", "knowledge.review",
    "knowledge.publish", "knowledge.revoke", "knowledge.answer", "knowledge.search",
    "evidence.read",
)
AGENT_SCOPES = ("knowledge.answer", "knowledge.search", "evidence.read")


def api_base(record: dict[str, Any]) -> str:
    return f"http://127.0.0.1:{record['api_port']}"


def upload(record: dict[str, Any], token: str, *, title: str, text: str,
           domain: str, key: str) -> str:
    body = {
        "filename": f"{key}.md", "title": title, "domain": domain,
        "owner_department": "ops", "classification": 1, "audience": "internal",
        "required_scope": "internal_product", "acl_allow_roles": ["staff"],
        "knowledge_key": key,
        "content_base64": base64.b64encode(text.encode()).decode(),
        **({"catalog_currency": "CNY"} if domain == "product_catalog" else {}),
    }
    code, payload = http("POST", f"{api_base(record)}/v1/knowledge/documents",
                          payload=body, token=token)
    if code != 201:
        raise SystemExit(f"导入失败 {code}: {payload}")
    version_id = str(payload["version_id"])
    review_code, review_body = http(
        "POST", f"{api_base(record)}/v1/knowledge/{version_id}/review",
        payload={"decision": "PASS"}, token=token,
    )
    if review_code != 200:
        raise SystemExit(f"审核失败 {review_code}: {review_body}")
    publish_code, publish_body = http(
        "POST", f"{api_base(record)}/v1/knowledge/{version_id}/publish",
        payload={"audience": "internal"}, token=token,
    )
    if publish_code != 200:
        raise SystemExit(f"发布失败 {publish_code}: {publish_body}")
    return version_id


def answer(record: dict[str, Any], token: str, query: str) -> dict[str, Any]:
    """调用问答；非 200 时把 HTTP 码放进返回值，由调用方决定是否可接受。

    （生成链路依赖本机模型；本次演练固定词法检索，未参与模型服务，
    因此"撤销后是否拒答"以检索与证据通道为准，问答结果仅作参考记录。）
    """
    code, payload = http(
        "POST", f"{api_base(record)}/v1/knowledge/answer",
        payload={"query": query, "knowledge_scope": "internal_product",
                 "language": "zh-CN", "max_evidence": 5},
        token=token,
    )
    payload = dict(payload)
    payload["_http"] = code
    return payload


def provision(cfg: dict[str, Any]) -> dict[str, Any]:
    if cfg["workdir"].exists():
        shutil.rmtree(cfg["workdir"])
    run([
        sys.executable, "-m", "fkh.tools.instance", "provision",
        "--workdir", str(cfg["workdir"]),
        "--db-port", str(cfg["db_port"]),
        "--api-port", str(cfg["api_port"]),
        "--db-name", cfg["db_name"],
        "--name", cfg["name"],
    ])
    return json.loads((cfg["workdir"] / "instance.json").read_text(encoding="utf-8"))


def serve(cfg: dict[str, Any]) -> None:
    # 验收追求可复跑：检索固定为词法通道，避免依赖本机模型服务（向量通道另行验证）
    run(
        [sys.executable, "-m", "fkh.tools.instance", "serve", "--workdir", str(cfg["workdir"])],
        env_extra={
            # 检索固定词法通道：演练不依赖向量模型，结果可复现
            "FKH_RETRIEVAL_MODE": "lexical",
            # 生成走本机 Ollama（撤销后的"不可答"要经过真实生成判定链路）
            "FKH_LLM_PROVIDER": "ollama",
            "FKH_LLM_MODEL": "qwen3:14b",
            "FKH_LLM_BASE_URL": "http://127.0.0.1:11434",
            "FKH_LLM_TIMEOUT_SECONDS": "180",
        },
    )


def stop(cfg: dict[str, Any]) -> None:
    run([sys.executable, "-m", "fkh.tools.instance", "stop", "--workdir", str(cfg["workdir"])])


def role_flags(dsn: str) -> dict[str, Any]:
    import psycopg

    with psycopg.connect(dsn) as conn:
        role = conn.execute("SELECT current_user").fetchone()
        assert role is not None
        name = str(role[0])
        row = conn.execute(
            "SELECT rolsuper, rolbypassrls, rolcreatedb, rolcreaterole FROM pg_roles"
            " WHERE rolname = current_user"
        ).fetchone()
        owner = conn.execute(
            "SELECT tableowner FROM pg_tables WHERE tablename = 'knowledge_versions'"
        ).fetchone()
    assert row is not None and owner is not None
    return {
        "role": name,
        "rolsuper": bool(row[0]),
        "rolbypassrls": bool(row[1]),
        "rolcreatedb": bool(row[2]),
        "rolcreaterole": bool(row[3]),
        "table_owner": str(owner[0]),
    }


def main() -> int:
    BASE.mkdir(parents=True, exist_ok=True)

    # ---------- ① 空环境首装 ----------
    record_a = provision(A)
    step("provision-a", True, f"db_port={A['db_port']} api_port={A['api_port']}")
    flags = role_flags(record_a["app_dsn"])
    step("app-role-minimal", not flags["rolsuper"] and not flags["rolbypassrls"]
         and flags["role"] != flags["table_owner"],
         json.dumps(flags, ensure_ascii=False))
    serve(A)
    step("serve-a", True, f"http://127.0.0.1:{A['api_port']}")

    secret_a = str(record_a["jwt_secret"])
    ops_a = mint(secret_a, record_a, principal=record_a["ops_principal"], scopes=OPS_SCOPES)
    agent_a = mint(secret_a, record_a, principal=record_a["agent_principal"], scopes=AGENT_SCOPES)

    upload(record_a, ops_a, title="合成客户目录", text=CATALOG,
           domain="product_catalog", key="acc-catalog")
    safety_version = upload(record_a, ops_a, title="客户现场安全守则", text=SAFETY,
                            domain="equipment_safety", key="acc-safety")
    assert safety_version
    quality_version = upload(record_a, ops_a, title="客户质量条款", text=QUALITY,
                             domain="quality", key="acc-quality")
    step("import-and-publish", True, f"3 docs, version={quality_version[:8]}…")

    answer_before = answer(record_a, agent_a, "CX-1001 的单价是多少？")
    citations = answer_before.get("citations") or []
    step("agent-answer-with-citation",
         answer_before["status"] == "ANSWERED" and bool(citations),
         f"status={answer_before['status']} citations={len(citations)}")
    old_evidence = str(citations[0]["evidence_id"]) if citations else ""
    code, _ = http("GET", f"{api_base(record_a)}/v1/evidence/{old_evidence}", token=agent_a)
    step("old-evidence-locatable", code == 200, f"GET /v1/evidence → {code}")

    ingest_denied, _ = http(
        "POST", f"{api_base(record_a)}/v1/knowledge/documents",
        payload={"filename": "x.md", "title": "x", "domain": "quality",
                 "owner_department": "ops", "classification": 1, "audience": "internal",
                 "required_scope": "internal_product", "acl_allow_roles": ["staff"],
                 "knowledge_key": "agent-denied",
                 "content_base64": base64.b64encode(b"x").decode()},
        token=agent_a,
    )
    step("agent-cannot-ingest", ingest_denied == 403, f"HTTP {ingest_denied}")

    # ---------- ② 重启同一实例 ----------
    stop(A)
    serve(A)
    code, domains = http("GET", f"{api_base(record_a)}/v1/config/domains", token=ops_a)
    step("restart-token-and-config",
         code == 200 and len(domains.get("domains", [])) >= 4,
         f"config/domains → {code}, {len(domains.get('domains', []))} 个域")
    answer_after_restart = answer(record_a, agent_a, "CX-1002 的单价是多少？")
    step("restart-data-intact", answer_after_restart["status"] == "ANSWERED",
         f"status={answer_after_restart['status']}")
    code, _ = http("GET", f"{api_base(record_a)}/v1/evidence/{old_evidence}", token=agent_a)
    step("restart-evidence-still-valid", code == 200, f"HTTP {code}")

    # ---------- ③ 撤销 → 备份 → 恢复到全新隔离实例 ----------
    pre_search, pre_body = http(
        "POST", f"{api_base(record_a)}/v1/knowledge/search",
        payload={"query": "批次抽检比例", "knowledge_scope": "internal_product",
                 "max_results": 5},
        token=agent_a,
    )
    pre_hits = [h for h in (pre_body.get("results") or []) if str(h.get("version_id")) == quality_version]
    revoked_chunk = str(pre_hits[0]["chunk_id"]) if pre_hits else ""
    code, _ = http("GET", f"{api_base(record_a)}/v1/evidence/{revoked_chunk}", token=agent_a)
    step("revoked-evidence-readable-before", code == 200, f"GET /v1/evidence → {code}")

    revoke_code, revoke_body = http(
        "POST", f"{api_base(record_a)}/v1/knowledge/{quality_version}/revoke?reason=acceptance",
        payload={}, token=ops_a,
    )
    step("revoke-quality", revoke_code == 200, f"HTTP {revoke_code}")
    # 撤销后"不可检索"用确定性通道验证（检索 + 证据直读），不依赖生成模型
    search_code, search_body = http(
        "POST", f"{api_base(record_a)}/v1/knowledge/search",
        payload={"query": "批次抽检比例", "knowledge_scope": "internal_product",
                 "max_results": 5},
        token=agent_a,
    )
    hits = (search_body.get("results") or []) if search_code == 200 else []
    revoked_hits = [h for h in hits if str(h.get("version_id")) == quality_version]
    step("revoked-not-searchable", search_code == 200 and not revoked_hits,
         f"search → {search_code}, 命中 {len(hits)} 条，其中已撤销版本 {len(revoked_hits)} 条")

    code, _ = http("GET", f"{api_base(record_a)}/v1/evidence/{revoked_chunk}", token=agent_a)
    step("revoked-evidence-rejected", code in (403, 404), f"GET /v1/evidence → {code}")

    revoked_answer = answer(record_a, agent_a, "批次抽检比例是多少？")
    step("revoked-not-answerable",
         revoked_answer.get("_http") == 200 and revoked_answer.get("status") != "ANSWERED",
         f"http={revoked_answer.get('_http')} status={revoked_answer.get('status')}")

    backup_path = BASE / "instance-a.dump"
    run([sys.executable, "-m", "fkh.tools.instance", "backup",
         "--workdir", str(A["workdir"]), "--out", str(backup_path)])
    step("backup", backup_path.exists(), f"{backup_path.stat().st_size} bytes")

    run([sys.executable, "-m", "fkh.tools.instance", "restore",
         "--from", str(backup_path), "--workdir", str(B["workdir"]),
         "--db-port", str(B["db_port"]), "--api-port", str(B["api_port"]),
         "--db-name", B["db_name"], "--name", B["name"]])
    record_b = json.loads((B["workdir"] / "instance.json").read_text(encoding="utf-8"))
    step("restore-new-instance",
         record_b["jwt_secret"] != record_a["jwt_secret"],
         f"db_port={B['db_port']} api_port={B['api_port']} secret=新生成")
    serve(B)

    secret_b = str(record_b["jwt_secret"])
    ops_b = mint(secret_b, record_b, principal=record_b["ops_principal"], scopes=OPS_SCOPES)
    agent_b = mint(secret_b, record_b, principal=record_b["agent_principal"], scopes=AGENT_SCOPES)
    # 恢复实例的管理令牌用于读取域配置，确认配置随数据库一起恢复
    assert http("GET", f"{api_base(record_b)}/v1/config/domains", token=ops_b)[0] == 200

    cross_code, _ = http("GET", f"{api_base(record_b)}/v1/config/domains", token=agent_a)
    step("cross-instance-token-rejected", cross_code == 401, f"A 的令牌打 B → {cross_code}")

    restored_answer = answer(record_b, agent_b, "CX-1001 的单价是多少？")
    step("restored-valid-doc-answerable",
         restored_answer["status"] == "ANSWERED" and bool(restored_answer.get("citations")),
         f"status={restored_answer['status']} citations={len(restored_answer.get('citations') or [])}")

    code, _ = http("GET", f"{api_base(record_b)}/v1/evidence/{revoked_chunk}", token=agent_b)
    step("restored-revoked-evidence-rejected", code in (403, 404), f"HTTP {code}")

    code, _ = http("GET", f"{api_base(record_b)}/v1/evidence/{old_evidence}", token=agent_b)
    step("restored-valid-evidence-readable", code == 200, f"HTTP {code}")

    restored_search, restored_body = http(
        "POST", f"{api_base(record_b)}/v1/knowledge/search",
        payload={"query": "批次抽检比例", "knowledge_scope": "internal_product",
                 "max_results": 5},
        token=agent_b,
    )
    restored_hits = (restored_body.get("results") or []) if restored_search == 200 else []
    leaked = [h for h in restored_hits if str(h.get("version_id")) == quality_version]
    step("restored-revoked-not-searchable",
         restored_search == 200 and not leaked,
         f"search → {restored_search}, 命中 {len(restored_hits)} 条，已撤销版本 {len(leaked)} 条")
    restored_revoked_answer = answer(record_b, agent_b, "批次抽检比例是多少？")
    step("restored-revoked-not-answerable",
         restored_revoked_answer.get("_http") == 200
         and restored_revoked_answer.get("status") != "ANSWERED",
         f"http={restored_revoked_answer.get('_http')} "
         f"status={restored_revoked_answer.get('status')}")

    import psycopg

    with psycopg.connect(record_b["admin_dsn"]) as conn:
        conn.execute("SELECT set_config('app.tenant_id', %s, false)", (record_b["tenant_id"],))
        counts = {}
        for table in ("rag_queries", "evidence_access_log", "knowledge_versions", "catalog_items"):
            row = conn.execute(
                f"SELECT count(*) FROM {table} WHERE tenant_id = %s", (record_b["tenant_id"],)
            ).fetchone()
            counts[table] = int(row[0]) if row else 0
        revoked_row = conn.execute(
            "SELECT status FROM knowledge_versions WHERE tenant_id = %s AND id = %s",
            (record_b["tenant_id"], quality_version),
        ).fetchone()
    step("restored-audit-retained",
         counts["rag_queries"] > 0 and counts["evidence_access_log"] > 0,
         json.dumps({**counts, "quality_status": revoked_row[0] if revoked_row else None},
                    ensure_ascii=False))

    stop(A)
    stop(B)
    step("cleanup", True, "两套临时实例已停止（数据目录保留在 .acceptance/）")

    write_evidence(record_a, record_b, counts)
    failed = [r for r in RECORDS if isinstance(r, dict) and "step" in r and not r["ok"]]
    print(json.dumps({"summary": {"steps": len([r for r in RECORDS if 'step' in r]),
                                  "failed": len(failed)}}, ensure_ascii=False))
    return 1 if failed else 0


def write_evidence(record_a: dict[str, Any], record_b: dict[str, Any],
                   counts: dict[str, int]) -> None:
    lines = [
        "# 空环境首装 → 重启 → 备份恢复 验收记录",
        "",
        f"- 时间：{datetime.now(UTC).isoformat()}",
        "- 基准：`08c2f0d`（ADR-009：一客户一套独立私域实例；共用 RAG 基座与 Knowledge API）",
        "- 说明：全部使用**临时合成实例**；工厂演示（8010/8000/55432）与烘焙演示（8114/8104/55462）未触碰。",
        "",
        "## 实例",
        "",
        "| | 源实例 A | 恢复实例 B |",
        "|---|---|---|",
        f"| 数据目录 | `{Path(record_a['workdir']).name}` | `{Path(record_b['workdir']).name}` |",
        f"| DB 端口 | {record_a['db_port']} | {record_b['db_port']} |",
        f"| API 端口 | {record_a['api_port']} | {record_b['api_port']} |",
        f"| 业务角色 | `{record_a['app_role']}`（非 owner，无 BYPASSRLS） | `{record_b['app_role']}` |",
        "| JWT 密钥 | 实例独立（重启后稳定） | **新生成**（与 A 不通用） |",
        f"| 租户 | `{record_a['tenant_id']}` | `{record_b['tenant_id']}`（恢复） |",
        "",
        "## 步骤与结果",
        "",
        "| 步骤 | 结果 | 细节 |",
        "|---|---|---|",
    ]
    for item in RECORDS:
        if "step" not in item:
            continue
        mark = "✅" if item["ok"] else "❌"
        detail = str(item["detail"]).replace("|", "\\|")
        lines.append(f"| {item['step']} | {mark} | {detail} |")

    lines += ["", "## 实际命令（退出码）", "", "```text"]
    for item in RECORDS:
        if "cmd" not in item:
            continue
        tail = f"  → {item['out'][0]}" if item["out"] else ""
        lines.append(f"$ {item['cmd']}{tail}\n  exit={item['exit']}")
    lines += ["```", "", "## 恢复后审计计数（实例 B）", "", "```json",
              json.dumps(counts, ensure_ascii=False, indent=2), "```", ""]
    lines += [
        "## 未覆盖的边界",
        "",
        "1. 备份是**逻辑备份**（pg_dump 自定义格式），不含角色与授权；恢复时由 `instance restore` 重建业务角色，",
        "   未验证跨大版本升级或物理备份（pg_basebackup）。",
        "2. 备份为**在线一致性快照**，未验证「备份过程中持续写入」的恢复语义（未做 PITR）。",
        "3. 恢复后的实例沿用同一 JWT 密钥格式但**新密钥**，未验证「密钥轮换后旧令牌失效」的策略。",
        "4. 迁移只验证了当前最新版本；未演练从旧版本恢复到新版本（升级路径由 `docs/UPGRADE-DRILL.md` 另行覆盖）。",
        "5. 未验证多租户在同一实例内的表现（产品边界是一客户一实例，不做多租户 SaaS）。",
    ]
    EVIDENCE.write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
