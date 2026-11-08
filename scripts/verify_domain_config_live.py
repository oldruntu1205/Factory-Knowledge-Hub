"""实例级知识域实机验证（2026-11-08，ADR-009 + 0035）。

在实例 B（烘焙设备，端口 8104/8114）上验证：
1. 新代码下实例已带本实例域清单（demo 模板 8 域）；
2. 登记行业自定义域（`roaster_maintenance`）→ 201；
3. 用该域导入资料 → 能审核发布 → 能按内文回答并给出引用；
4. 未登记域导入 → 422（提示登记方式）。

输出 JSON 行，写入 `_evidence/domain_config_live.txt`。
"""

from __future__ import annotations

import base64
import json
import sys
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta

import jwt

B_SECRET = "instance-roaster-secret-0123456789"
B_UI = "http://127.0.0.1:8114"
B_API = "http://127.0.0.1:8104"

DOC = """烘焙机保养手册（实机验证样本）

每烘焙 50 锅清理一次银皮收集盒，避免积粉影响风道。
滚筒轴承每季度加注高温润滑脂一次。
"""


def _request(url: str, *, payload: dict | None = None, token: str | None = None,
             method: str | None = None) -> tuple[int, dict]:
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=data, headers=headers,
                                     method=method or ("POST" if data else "GET"))
    try:
        with urllib.request.urlopen(request, timeout=300) as resp:
            return resp.status, json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode() or "{}")


def _token(status: dict, scopes: tuple[str, ...]) -> str:
    now = datetime.now(UTC)
    return jwt.encode(
        {
            "iss": "fkh.local", "aud": "fkh-api", "sub": status["subject"],
            "iat": now, "exp": now + timedelta(minutes=30),
            "tenant_id": status["tenant_id"], "principal_id": status["principal_id"],
            "roles": ["staff"], "scope": list(scopes),
            "allowed_scopes": ["internal_product", "customer_public"],
        },
        B_SECRET,
        algorithm="HS256",
    )


def main() -> int:
    _, status = _request(f"{B_UI}/api/status")
    writer = _token(status, ("knowledge.read", "knowledge.write", "knowledge.ingest",
                             "knowledge.review", "knowledge.publish", "knowledge.answer",
                             "knowledge.search", "evidence.read"))

    code, listed = _request(f"{B_API}/v1/config/domains", token=writer)
    print(json.dumps({"step": "list", "http": code,
                      "domains": [d["code"] for d in listed.get("domains", [])]},
                     ensure_ascii=False))

    unregistered = {
        "filename": "roaster.md", "title": "烘焙机保养", "domain": "roaster_maintenance",
        "owner_department": "ops", "classification": 1, "audience": "internal",
        "required_scope": "internal_product", "acl_allow_roles": ["staff"],
        "knowledge_key": "live-roaster-maintenance",
        "content_base64": base64.b64encode(DOC.encode()).decode(),
    }
    blocked_code, blocked = _request(f"{B_API}/v1/knowledge/documents",
                                     payload=unregistered, token=writer)
    print(json.dumps({"step": "upload_unregistered", "http": blocked_code,
                      "message": (blocked.get("error") or {}).get("message")},
                     ensure_ascii=False))

    reg_code, reg = _request(f"{B_API}/v1/config/domains",
                             payload={"code": "roaster_maintenance", "label": "烘焙机维护"},
                             token=writer)
    print(json.dumps({"step": "register", "http": reg_code, "body": reg}, ensure_ascii=False))

    up_code, uploaded = _request(f"{B_API}/v1/knowledge/documents",
                                 payload=unregistered, token=writer)
    print(json.dumps({"step": "upload_registered", "http": up_code,
                      "version": uploaded.get("version_id")}, ensure_ascii=False))
    version_id = uploaded.get("version_id")
    if version_id:
        r1 = _request(f"{B_API}/v1/knowledge/{version_id}/review",
                      payload={"decision": "PASS"}, token=writer)[0]
        r2 = _request(f"{B_API}/v1/knowledge/{version_id}/publish",
                      payload={"audience": "internal"}, token=writer)[0]
        print(json.dumps({"step": "approve_publish", "review": r1, "publish": r2}))

    agent = _token(status, ("knowledge.answer", "knowledge.search", "evidence.read"))
    ans_code, answer = _request(
        f"{B_API}/v1/knowledge/answer",
        payload={"query": "银皮收集盒多久清理一次？", "knowledge_scope": "internal_product",
                 "language": "zh-CN", "max_evidence": 5},
        token=agent,
    )
    print(json.dumps({"step": "answer", "http": ans_code, "status": answer.get("status"),
                      "answer": (answer.get("answer") or "")[:60],
                      "citations": len(answer.get("citations") or [])}, ensure_ascii=False))

    ok = (code == 200 and blocked_code == 422 and reg_code == 201
          and up_code == 201 and answer.get("status") == "ANSWERED"
          and answer.get("citations"))
    print(json.dumps({"step": "verdict", "pass": bool(ok)}, ensure_ascii=False))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
