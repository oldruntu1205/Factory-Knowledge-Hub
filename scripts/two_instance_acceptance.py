"""双实例实机验收脚本（2026-11-08）。

用**同一提交**启动的两套独立实例（A=工厂，B=烘焙设备），各自用最小权限 Agent 令牌：
1. 各自问答并拿到引用；
2. 跨实例拒绝（A 的令牌打 B、B 的令牌打 A，均应 401）；
3. 单边撤销（撤销 A 的知识后，A 不可答，B 不受影响）。

输出为 JSON 行，便于人工核对与存档。
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta

import jwt

A_SECRET = "demo-jwt-secret-0123456789abcdef"
B_SECRET = "instance-roaster-secret-0123456789"


def _get(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=10) as resp:
        return json.loads(resp.read().decode())


def _post(url: str, payload: dict, token: str) -> tuple[int, dict]:
    data = json.dumps(payload).encode()
    request = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json",
                                 "Authorization": f"Bearer {token}"}, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=300) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode() or "{}")


def _get_auth(url: str, token: str) -> tuple[int, dict]:
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(request, timeout=60) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode() or "{}")


def _token(secret: str, status: dict, *, allowed: tuple[str, ...]) -> str:
    now = datetime.now(UTC)
    return jwt.encode(
        {
            "iss": "fkh.local", "aud": "fkh-api", "sub": status["subject"],
            "iat": now, "exp": now + timedelta(minutes=30),
            "tenant_id": status["tenant_id"], "principal_id": status["principal_id"],
            "roles": ["staff"],
            "scope": ["knowledge.answer", "knowledge.search", "evidence.read"],
            "allowed_scopes": list(allowed),
        },
        secret,
        algorithm="HS256",
    )


def main() -> int:
    a = _get("http://127.0.0.1:8010/api/status")
    b = _get("http://127.0.0.1:8114/api/status")
    print(json.dumps({"step": "status", "A": {k: a.get(k) for k in ("tenant_id", "subject")},
                      "B": {k: b.get(k) for k in ("tenant_id", "subject")}}, ensure_ascii=False))

    # 最小权限 Agent：只给问答/检索/证据读取，数据范围只给本实例需要的 internal_product
    agent_a = _token(A_SECRET, a, allowed=("internal_product",))
    agent_b = _token(B_SECRET, b, allowed=("internal_product",))

    status_a, body_a = _post(
        "http://127.0.0.1:8000/v1/knowledge/answer",
        {"query": "A112420 的单价是多少？", "knowledge_scope": "internal_product",
         "language": "zh-CN", "max_evidence": 5},
        agent_a,
    )
    print(json.dumps({"step": "A_answer", "http": status_a, "status": body_a.get("status"),
                      "answer": (body_a.get("answer") or "")[:60],
                      "citations": len(body_a.get("citations") or [])}, ensure_ascii=False))

    status_b, body_b = _post(
        "http://127.0.0.1:8104/v1/knowledge/answer",
        {"query": "烘焙机银皮收集盒多久清理一次？", "knowledge_scope": "internal_product",
         "language": "zh-CN", "max_evidence": 5},
        agent_b,
    )
    print(json.dumps({"step": "B_answer", "http": status_b, "status": body_b.get("status"),
                      "answer": (body_b.get("answer") or "")[:60],
                      "citations": len(body_b.get("citations") or [])}, ensure_ascii=False))

    # 工厂问题的货号在 B 上不应答（B 只导入了烘焙资料）
    status_bx, body_bx = _post(
        "http://127.0.0.1:8104/v1/knowledge/answer",
        {"query": "A112420 的单价是多少？", "knowledge_scope": "internal_product",
         "language": "zh-CN", "max_evidence": 5},
        agent_b,
    )
    print(json.dumps({"step": "B_factory_question", "http": status_bx,
                      "status": body_bx.get("status")}, ensure_ascii=False))

    # 跨实例拒绝：A 的令牌打 B、B 的令牌打 A
    cross_ab, cross_ab_body = _post(
        "http://127.0.0.1:8104/v1/knowledge/search",
        {"query": "银皮", "knowledge_scope": "internal_product", "max_results": 3},
        agent_a,
    )
    print(json.dumps({"step": "A_token_on_B", "http": cross_ab,
                      "error": (cross_ab_body.get("error") or {}).get("code")},
                     ensure_ascii=False))
    cross_ba, cross_ba_body = _post(
        "http://127.0.0.1:8000/v1/knowledge/search",
        {"query": "A112420", "knowledge_scope": "internal_product", "max_results": 3},
        agent_b,
    )
    print(json.dumps({"step": "B_token_on_A", "http": cross_ba,
                      "error": (cross_ba_body.get("error") or {}).get("code")},
                     ensure_ascii=False))

    # 单边撤销：用 A 的内部身份（带 revoke 权限）撤销 A 的目录版本
    admin_a = jwt.encode(
        {
            "iss": "fkh.local", "aud": "fkh-api", "sub": a["subject"],
            "iat": datetime.now(UTC), "exp": datetime.now(UTC) + timedelta(minutes=30),
            "tenant_id": a["tenant_id"], "principal_id": a["principal_id"],
            "roles": ["staff"],
            "scope": ["knowledge.read", "knowledge.revoke", "knowledge.search"],
            "allowed_scopes": ["internal_product", "customer_public"],
        },
        A_SECRET,
        algorithm="HS256",
    )
    listing_status, listing = _get_auth("http://127.0.0.1:8000/v1/knowledge/documents", admin_a)
    items = listing.get("items") or listing.get("documents") or []
    # A 里可能有多个目录来源（历史演练留下的合成目录），全部撤销后 A 才真正"不可答"
    catalogs = [i for i in items if i["domain"] == "product_catalog"
                and i["status"] in {"PUBLISHED_PUBLIC", "APPROVED_INTERNAL"}]
    revoked = []
    for target in catalogs:
        code, _body = _post(
            f"http://127.0.0.1:8000/v1/knowledge/{target['version_id']}/revoke?reason=live-single-side",
            {}, admin_a,
        )
        revoked.append({"version": target["version_id"], "http": code})
    print(json.dumps({"step": "A_revoke", "count": len(revoked), "results": revoked},
                     ensure_ascii=False))

    after_a, after_a_body = _post(
        "http://127.0.0.1:8000/v1/knowledge/answer",
        {"query": "A112420 的单价是多少？", "knowledge_scope": "internal_product",
         "language": "zh-CN", "max_evidence": 5},
        agent_a,
    )
    print(json.dumps({"step": "A_after_revoke", "http": after_a,
                      "status": after_a_body.get("status")}, ensure_ascii=False))

    after_b, after_b_body = _post(
        "http://127.0.0.1:8104/v1/knowledge/answer",
        {"query": "滚筒轴承多久加一次润滑脂？", "knowledge_scope": "internal_product",
         "language": "zh-CN", "max_evidence": 5},
        agent_b,
    )
    print(json.dumps({"step": "B_after_A_revoke", "http": after_b,
                      "status": after_b_body.get("status"),
                      "citations": len(after_b_body.get("citations") or [])},
                     ensure_ascii=False))

    ok = (
        status_a == 200 and body_a.get("status") == "ANSWERED"
        and body_b.get("status") == "ANSWERED"
        and cross_ab == 401 and cross_ba == 401
        and after_a_body.get("status") != "ANSWERED"
        and after_b_body.get("status") == "ANSWERED"
    )
    print(json.dumps({"step": "verdict", "pass": ok}, ensure_ascii=False))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
