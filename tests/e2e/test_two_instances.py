"""双实例验收：同一提交、两个隔离实例（单用户私域部署形态）。

架构决策（2026-11-08，用户确认）：产品是**可复用的 RAG 知识基座**，交付形态为
**单用户私域部署、一客户一套独立实例**；各客户复用同一套代码与 Knowledge API，
各自管理资料、索引、凭据、配置、审计；每套实例可接入多个 Agent 按用途授权。
多租户 SaaS / 跨客户管理 / 计费不在范围内（`tenant_id` 保留兼容）。

本文件用两个**独立数据库 + 独立业务角色 + 独立 JWT 密钥**模拟两套实例：
- 实例 A：工厂资料（目录 + 检验规范），客服 Agent（customer_public）
- 实例 B：非工厂合成资料（咖啡烘焙设备维护），维护 Agent（internal_product）
验收：B 只需换配置/导资料/接 Agent；A 的凭据进不了 B；A 撤销不影响 B；
并记录"真正阻碍复用的地方"（域枚举等工厂专属常量）。
"""

from __future__ import annotations

import base64
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlparse

import jwt
import psycopg
import pytest
from fastapi.testclient import TestClient
from migrations.apply import apply_all, discover_migrations, fetch_applied

from fkh.api.main import create_app
from fkh.config import Settings

pytestmark = pytest.mark.requires_pg

SECRET_A = "instance-a-secret-0123456789"
SECRET_B = "instance-b-secret-0123456789"
HASH_A = "instance-a-hmac-0123456789"
HASH_B = "instance-b-hmac-0123456789"

FACTORY_CATALOG = (
    "第 1 页\n\n货号 规格 管径*总长 上标口*下标口 单价\n"
    "A112420 200mm 38*300 24*24 39.00\n"
)
FACTORY_QC = "冷凝管检验规范\n\n气密性检验：充气至 0.2MPa，保压 60 秒为合格。\n"
ROASTER_CATALOG = (
    "第 1 页\n\n货号 规格 滚筒容量 电压 单价\n"
    "B220310 5kg 5kg 220V 12800.00\n"
)
ROASTER_MANUAL = "烘焙机保养手册\n\n每烘焙 50 锅清理一次银皮收集盒；轴承每季度加润滑脂。\n"


class _Scripted:
    model_id = "scripted"

    def generate(self, prompt: str, *, max_tokens: int = 512, temperature: float = 0.0) -> Any:
        from dataclasses import dataclass as _dc

        @_dc
        class _Generated:
            text: str
            model_id: str = "scripted"

        if "意图分类器" in prompt:
            return _Generated(text="否")
        import re as _re

        found = _re.search(r'<evidence id="([0-9a-f-]{36})"', prompt)
        cite = found.group(1) if found else "无"
        if "银皮" in prompt or "润滑" in prompt:
            answer = "每烘焙 50 锅清理一次银皮收集盒；轴承每季度加润滑脂。"
        elif "规格" in prompt:
            answer = "该型号规格为 200mm 38*300 24*24。"
        else:
            answer = "无法回答。"
        return _Generated(text=f"{answer}\n引用：{cite}")


def _token(secret: str, tenant: str, principal: str, *, scopes: tuple[str, ...],
           allowed: tuple[str, ...], roles: tuple[str, ...] = ("staff",)) -> str:
    now = datetime.now(UTC)
    return jwt.encode(
        {
            "iss": "fkh.local", "aud": "fkh-api", "sub": f"{principal[:8]}@{tenant[:4]}.test",
            "iat": now, "exp": now + timedelta(minutes=20), "tenant_id": tenant,
            "principal_id": principal, "roles": list(roles), "scope": list(scopes),
            "allowed_scopes": list(allowed),
        },
        secret,
        algorithm="HS256",
    )


def _scratch(pg_url: str) -> str:
    parsed = urlparse(pg_url)
    db = f"fkh_inst_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(pg_url, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{db}"')
    return f"postgresql://{parsed.username or 'postgres'}@{parsed.hostname}:{parsed.port}/{db}"


def _migrate(dsn: str) -> None:
    with psycopg.connect(dsn) as conn:
        apply_all(conn, discover_migrations(), fetch_applied(conn), dry_run=False)


def _role(dsn: str, prefix: str) -> str:
    name = f"{prefix}_{uuid.uuid4().hex[:8]}"
    parsed = urlparse(dsn)
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(f"CREATE ROLE {name} LOGIN")
        admin.execute(f"GRANT USAGE ON SCHEMA public TO {name}")
        admin.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {name}")
        admin.execute(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {name}")
        admin.execute(f"GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA public TO {name}")
    return f"postgresql://{name}@{parsed.hostname}:{parsed.port}/{parsed.path.lstrip('/')}"


def _seed_tenant(dsn: str) -> dict[str, str]:
    """在实例自己的库里建租户与主体（数据库由该实例独占）。"""
    tenant = uuid.uuid4()
    ops = uuid.uuid4()
    customer = uuid.uuid4()
    with psycopg.connect(dsn) as conn:
        conn.execute("INSERT INTO tenants (id, name) VALUES (%s, '单实例租户')", (tenant,))
        for pid, subject in ((ops, "ops@instance.test"), (customer, "agent@instance.test")):
            conn.execute(
                "INSERT INTO principals (tenant_id, id, external_subject, principal_type)"
                " VALUES (%s, %s, %s, 'user')",
                (tenant, pid, subject),
            )
        conn.commit()
    return {"tenant": str(tenant), "ops": str(ops), "customer": str(customer)}


@pytest.fixture(scope="module")
def instances(pg_url: str) -> Iterator[dict[str, Any]]:
    a_dsn = _scratch(pg_url)
    b_dsn = _scratch(pg_url)
    _migrate(a_dsn)
    _migrate(b_dsn)
    a_role = _role(a_dsn, "fkh_inst_a")
    b_role = _role(b_dsn, "fkh_inst_b")
    a_ids = _seed_tenant(a_dsn)
    b_ids = _seed_tenant(b_dsn)

    result = {
        "a": {"dsn": a_dsn, "app_dsn": a_role, "tenant": a_ids["tenant"],
              "ops": a_ids["ops"], "agent": a_ids["customer"], "secret": SECRET_A,
              "hash": HASH_A},
        "b": {"dsn": b_dsn, "app_dsn": b_role, "tenant": b_ids["tenant"],
              "ops": b_ids["ops"], "agent": b_ids["customer"], "secret": SECRET_B,
              "hash": HASH_B},
    }
    yield result
    for key in ("a", "b"):
        inst = result[key]
        role = urlparse(inst["app_dsn"]).username
        db = urlparse(inst["dsn"]).path.lstrip("/")
        # DROP OWNED 必须在角色拥有对象的那个库里执行，否则会报 DependentObjectsStillExist
        with psycopg.connect(inst["dsn"], autocommit=True) as own:
            own.execute(f"DROP OWNED BY {role}")
        with psycopg.connect(pg_url, autocommit=True) as admin:
            admin.execute(f"DROP ROLE IF EXISTS {role}")
            admin.execute(f'DROP DATABASE IF EXISTS "{db}" WITH (FORCE)')


def _app(inst: dict[str, Any]) -> TestClient:
    settings = Settings(
        jwt_secret=inst["secret"], database_url=inst["app_dsn"],
        retrieval_hash_key=inst["hash"], retrieval_hash_key_id=f"k-{inst['hash'][:6]}",
    )
    return TestClient(create_app(settings), raise_server_exceptions=False)


def _h(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _data_ops_token(inst: dict[str, Any]) -> str:
    return _token(inst["secret"], inst["tenant"], inst["ops"],
                  scopes=("knowledge.ingest", "knowledge.read", "knowledge.review",
                          "knowledge.publish", "knowledge.revoke", "knowledge.answer",
                          "knowledge.search", "evidence.read"),
                  allowed=("internal_product", "customer_public"))


def _public_approver_token(inst: dict[str, Any]) -> str:
    # 公开审批人必须与技术审批人不同主体：单实例里用同一主体会 409，这里仅走内部发布
    return _data_ops_token(inst)


def _import(client: TestClient, inst: dict[str, Any], *, title: str, text: str, domain: str,
            classification: int, audience: str, scope: str,
            currency: str | None = None) -> str:
    token = _data_ops_token(inst)
    body = {
        "filename": f"{uuid.uuid4().hex[:6]}.md", "title": title, "domain": domain,
        "owner_department": "ops", "classification": classification, "audience": audience,
        "required_scope": scope, "acl_allow_roles": ["staff", "customer"],
        "knowledge_key": f"inst-{uuid.uuid4().hex[:8]}",
        "content_base64": base64.b64encode(text.encode()).decode(),
        "catalog_currency": currency,
    }
    uploaded = client.post("/v1/knowledge/documents", json=body, headers=_h(token))
    assert uploaded.status_code == 201, uploaded.text
    version_id = uploaded.json()["version_id"]
    assert client.post(f"/v1/knowledge/{version_id}/review", json={"decision": "PASS"},
                       headers=_h(token)).status_code == 200
    assert client.post(f"/v1/knowledge/{version_id}/publish", json={"audience": "internal"},
                       headers=_h(token)).status_code == 200
    return version_id


def test_two_instances_serve_different_domains_without_code_changes(
    instances: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("fkh.api.main.get_generator", lambda *a, **k: _Scripted())
    monkeypatch.setattr("fkh.api.answer_exec.get_generator", lambda *a, **k: _Scripted())

    with _app(instances["a"]) as client_a:
        _import(client_a, instances["a"], title="对外目录", text=FACTORY_CATALOG,
                domain="product_catalog", classification=0, audience="customer_public",
                scope="customer_public", currency="CNY")
        _import(client_a, instances["a"], title="检验规范", text=FACTORY_QC,
                domain="quality", classification=1, audience="internal",
                scope="internal_product")

        ans_a = client_a.post(
            "/v1/knowledge/answer",
            json={"query": "A112420 的规格是什么？", "knowledge_scope": "customer_public",
                  "language": "zh-CN", "max_evidence": 5},
            headers=_h(_data_ops_token(instances["a"])),
        )
        assert ans_a.status_code == 200, ans_a.text
        assert ans_a.json()["status"] == "ANSWERED", ans_a.json()

    with _app(instances["b"]) as client_b:
        # 实例 B：完全不同的行业资料；只换 domain/文案与租户配置
        _import(client_b, instances["b"], title="烘焙机目录", text=ROASTER_CATALOG,
                domain="product_catalog", classification=0, audience="customer_public",
                scope="customer_public", currency="CNY")
        _import(client_b, instances["b"], title="烘焙机保养手册", text=ROASTER_MANUAL,
                domain="equipment_safety", classification=1, audience="internal",
                scope="internal_product")

        ans_b = client_b.post(
            "/v1/knowledge/answer",
            json={"query": "烘焙机银皮收集盒多久清理一次？", "knowledge_scope": "internal_product",
                  "language": "zh-CN", "max_evidence": 5},
            headers=_h(_data_ops_token(instances["b"])),
        )
        assert ans_b.status_code == 200, ans_b.text
        body_b = ans_b.json()
        assert body_b["status"] == "ANSWERED", body_b
        assert "50 锅" in body_b["answer"], body_b
        assert body_b["citations"], body_b


def test_credentials_do_not_cross_instances(instances: dict[str, Any]) -> None:
    a, b = instances["a"], instances["b"]
    token_a = _data_ops_token(a)

    # 1) A 的 JWT 在 B 上不认（密钥不同）
    with _app(b) as client_b:
        used = client_b.post(
            "/v1/knowledge/search",
            json={"query": "银皮", "knowledge_scope": "internal_product", "max_results": 5},
            headers=_h(token_a),
        )
        assert used.status_code == 401, used.text

    # 2) A 的数据库角色连到 B 的库上也没有数据权限（单实例=独立库+独立角色）
    parsed_a = urlparse(a["app_dsn"])
    b_db = urlparse(b["dsn"])
    cross_dsn = f"postgresql://{parsed_a.username}@{b_db.hostname}:{b_db.port}/{b_db.path.lstrip('/')}"
    with psycopg.connect(cross_dsn) as conn, pytest.raises(psycopg.errors.Error):
        conn.execute("SELECT count(*) FROM knowledge_chunks").fetchone()


def test_revoke_in_one_instance_does_not_affect_the_other(
    instances: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("fkh.api.main.get_generator", lambda *a, **k: _Scripted())
    a = instances["a"]
    with _app(a) as client_a, _app(instances["b"]) as client_b:
        # A 里撤销自己的检验规范
        listing = client_a.get("/v1/knowledge/documents", headers=_h(_data_ops_token(a)))
        assert listing.status_code == 200, listing.text
        payload = listing.json()
        items = payload.get("items") or payload.get("documents") or []
        target = next(item for item in items if item["title"] == "检验规范")
        revoked = client_a.post(
            f"/v1/knowledge/{target['version_id']}/revoke?reason=instance-isolation",
            headers=_h(_data_ops_token(a)),
        )
        assert revoked.status_code == 200, revoked.text

        after_a = client_a.post(
            "/v1/knowledge/answer",
            json={"query": "气密性检验的保压时间是多少？", "knowledge_scope": "internal_product",
                  "language": "zh-CN", "max_evidence": 5},
            headers=_h(_data_ops_token(a)),
        )
        assert after_a.json()["status"] != "ANSWERED"

        # B 完全不受影响
        after_b = client_b.post(
            "/v1/knowledge/answer",
            json={"query": "烘焙机银皮收集盒多久清理一次？", "knowledge_scope": "internal_product",
                  "language": "zh-CN", "max_evidence": 5},
            headers=_h(_data_ops_token(instances["b"])),
        )
        assert after_b.status_code == 200, after_b.text
        assert after_b.json()["status"] == "ANSWERED", after_b.json()


def test_domain_taxonomy_is_a_reuse_blocker(instances: dict[str, Any]) -> None:
    """如实记录复用阻碍：知识域枚举写死在库里（工厂域），换行业必须改代码/迁移。

    这里用"烘焙行业域"试探，期望**当前被拒**；等域清单移到应用层后，本用例应改为通过。
    """
    a = instances["a"]
    with _app(a) as client_a:
        body = {
            "filename": "bakery.md", "title": "烘焙设备", "domain": "bakery_equipment",
            "owner_department": "ops", "classification": 1, "audience": "internal",
            "required_scope": "internal_product", "acl_allow_roles": ["staff"],
            "knowledge_key": f"blocker-{uuid.uuid4().hex[:6]}",
            "content_base64": base64.b64encode(ROASTER_MANUAL.encode()).decode(),
        }
        response = client_a.post("/v1/knowledge/documents", json=body,
                                 headers=_h(_data_ops_token(a)))
    assert response.status_code == 422, response.text
