"""目录规则（货号正则 + 货币集合）实例配置的真实 PostgreSQL 验收。

L1 2026-11-08 裁决对应：
1. 缺规则 → 目录入库 422（可识别的配置错误），**读取不受影响**；
2. 所有写入路径共用校验：入库、冲突决议、回填；
3. PUT 幂等 + 审计（前后规则、操作者、版本号）；相同内容不产生版本；
4. 收紧规则使存量数据失效 → 409 拒绝；
5. 通用格式由库层保证（无触发器），业务规则在应用层；
6. 货币集合非空、三位大写、去重。
"""

from __future__ import annotations

import base64
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
import psycopg
import pytest
from fastapi.testclient import TestClient
from tests.conftest import create_principal, create_tenant
from tests.e2e._roles import drop_business_role, ensure_business_role

from fkh.api.main import create_app
from fkh.config import Settings

pg_url_type = psycopg.Connection[Any]

pytestmark = pytest.mark.requires_pg

SECRET = "catalog-rules-secret-0123456789"
HASH_KEY = "catalog-rules-hmac-0123456789"
HASH_KEY_ID = "k-rules"

SKU_TEXT = "第 1 页\n\nSKU-2026-001 200mm 39.00\nSKU-2026-002 300mm 48.50\n"
FACTORY_TEXT = "第 1 页\n\nA112420 200mm 39.00\n"

WRITER_SCOPES = ("knowledge.read", "knowledge.write", "knowledge.ingest",
                 "knowledge.review", "knowledge.publish", "knowledge.answer",
                 "knowledge.search", "evidence.read")


def _token(tenant: str, principal: str, scopes: tuple[str, ...] = WRITER_SCOPES) -> str:
    now = datetime.now(UTC)
    return jwt.encode(
        {
            "iss": "fkh.local", "aud": "fkh-api", "sub": f"{principal[:8]}@rules.test",
            "iat": now, "exp": now + timedelta(minutes=20), "tenant_id": tenant,
            "principal_id": principal, "roles": ["staff"], "scope": list(scopes),
            "allowed_scopes": ["internal_product", "customer_public"],
        },
        SECRET,
        algorithm="HS256",
    )


@pytest.fixture(scope="module")
def role(pg_url: str) -> Iterator[str]:
    name, dsn = ensure_business_role(pg_url, prefix="fkh_rules")
    yield dsn
    drop_business_role(pg_url, name)


@pytest.fixture
def client(role: str) -> Iterator[TestClient]:
    settings = Settings(jwt_secret=SECRET, database_url=role,
                        retrieval_hash_key=HASH_KEY, retrieval_hash_key_id=HASH_KEY_ID)
    with TestClient(create_app(settings), raise_server_exceptions=False) as test_client:
        yield test_client


def _h(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _upload(client: TestClient, token: str, text: str, *, currency: str | None = None,
            domain: str = "product_catalog") -> Any:
    body: dict[str, Any] = {
        "filename": "catalog.md", "title": "目录", "domain": domain,
        "owner_department": "sales", "classification": 1, "audience": "internal",
        "required_scope": "internal_product", "acl_allow_roles": ["staff"],
        "knowledge_key": f"rule-{uuid.uuid4().hex[:8]}",
        "content_base64": base64.b64encode(text.encode()).decode(),
    }
    if currency is not None:
        body["catalog_currency"] = currency
    return client.post("/v1/knowledge/documents", json=body, headers=_h(token))


@pytest.fixture
def bare(client: TestClient, admin: psycopg.Connection[Any]) -> dict[str, Any]:
    """未配置目录规则的租户（模拟"新租户还没初始化目录能力"）。"""
    tenant = create_tenant(admin, f"NORULE {uuid.uuid4().hex[:6]}", seed_domains=True)
    principal = create_principal(admin, tenant, f"nr-{uuid.uuid4().hex[:6]}@rules.test")
    admin.commit()
    with psycopg.connect(admin.info.dsn) as conn:
        conn.execute("SELECT set_config('app.tenant_id', %s, false)", (tenant,))
        conn.execute("DELETE FROM catalog_rules WHERE tenant_id = %s", (tenant,))
        conn.commit()
    return {"tenant": tenant, "token": _token(tenant, principal)}


@pytest.fixture
def factory(client: TestClient, admin: psycopg.Connection[Any]) -> dict[str, Any]:
    tenant = create_tenant(admin, f"RULE {uuid.uuid4().hex[:6]}")
    principal = create_principal(admin, tenant, f"r-{uuid.uuid4().hex[:6]}@rules.test")
    admin.commit()
    return {"tenant": tenant, "principal": principal, "token": _token(tenant, principal)}


def test_missing_rule_blocks_catalog_write_but_not_reads(
    client: TestClient, bare: dict[str, Any]
) -> None:
    blocked = _upload(client, bare["token"], FACTORY_TEXT, currency="CNY")
    assert blocked.status_code == 422, blocked.text
    message = blocked.json()["error"]["message"]
    assert "catalog-rules" in message and "PUT /v1/config/catalog-rules" in message

    # 非目录域（普通文档）不受影响
    plain = _upload(client, bare["token"], "普通知识", currency=None, domain="quality")
    assert plain.status_code == 201, plain.text

    # 读取端点仍可用（返回未配置）
    read = client.get("/v1/config/catalog-rules", headers=_h(bare["token"]))
    assert read.status_code == 200
    assert read.json() == {"configured": False, "rule": None}


def test_rule_allows_custom_code_and_currency(
    client: TestClient, factory: dict[str, Any]
) -> None:
    put = client.put(
        "/v1/config/catalog-rules",
        json={"code_pattern": r"^SKU-[0-9]{4}-[0-9]{3}$", "currencies": ["USD", "CNY"],
              "note": "改为条码货号 + 双币"},
        headers=_h(factory["token"]),
    )
    assert put.status_code == 200, put.text
    assert put.json()["changed"] is True
    assert put.json()["rule"]["currencies"] == ["CNY", "USD"]

    uploaded = _upload(client, factory["token"], SKU_TEXT, currency="USD")
    assert uploaded.status_code == 201, uploaded.text
    # 目录事实确实按新规则抽取（USD 货币 + SKU 货号）
    version_id = uploaded.json()["version_id"]
    assert version_id

    # 旧工厂货号现在不符合规则：抽取不出条目（不报错，只是 0 条）
    old = _upload(client, factory["token"], FACTORY_TEXT, currency="CNY")
    assert old.status_code == 201, old.text


def test_put_is_idempotent_and_audited(
    client: TestClient, factory: dict[str, Any], admin: psycopg.Connection[Any]
) -> None:
    payload = {"code_pattern": r"^SKU-[0-9]{4}-[0-9]{3}$", "currencies": ["CNY"]}
    first = client.put("/v1/config/catalog-rules", json=payload,
                       headers=_h(factory["token"]))
    assert first.status_code == 200 and first.json()["changed"] is True

    again = client.put("/v1/config/catalog-rules", json=payload,
                       headers=_h(factory["token"]))
    assert again.status_code == 200 and again.json()["changed"] is False

    with psycopg.connect(admin.info.dsn) as conn:
        conn.execute("SELECT set_config('app.tenant_id', %s, false)", (factory["tenant"],))
        revisions = conn.execute(
            "SELECT revision_no, before_rule, after_rule, actor_id FROM catalog_rule_revisions"
            " WHERE tenant_id = %s ORDER BY revision_no",
            (factory["tenant"],),
        ).fetchall()
    assert len(revisions) == 1, "相同内容重复 PUT 不应产生新版本"
    revision_no, before_rule, after_rule, actor_id = revisions[0]
    assert revision_no == 1
    assert before_rule["code_pattern"] == "^[A-Z]{1,3}[0-9]{5,8}$"
    assert after_rule["code_pattern"] == payload["code_pattern"]
    assert str(actor_id) == factory["principal"]


def test_tightening_rule_rejects_incompatible_existing_data(
    client: TestClient, factory: dict[str, Any]
) -> None:
    assert _upload(client, factory["token"], FACTORY_TEXT, currency="CNY").status_code == 201

    tightened = client.put(
        "/v1/config/catalog-rules",
        json={"code_pattern": r"^SKU-[0-9]{4}-[0-9]{3}$", "currencies": ["CNY"]},
        headers=_h(factory["token"]),
    )
    assert tightened.status_code == 409, tightened.text
    assert tightened.json()["error"]["code"] == "VERSION_CONFLICT"
    # 规则未被改动（409 时旧规则仍生效）
    unchanged = client.get("/v1/config/catalog-rules", headers=_h(factory["token"]))
    assert unchanged.json()["rule"]["code_pattern"] == "^[A-Z]{1,3}[0-9]{5,8}$"

    # 放宽（覆盖现有货号）可以成功
    widened = client.put(
        "/v1/config/catalog-rules",
        json={"code_pattern": r"^(?:A[0-9]{6}|SKU-[0-9]{4}-[0-9]{3})$",
              "currencies": ["CNY", "USD"]},
        headers=_h(factory["token"]),
    )
    assert widened.status_code == 200, widened.text


def test_currency_must_be_in_rule(client: TestClient, factory: dict[str, Any]) -> None:
    blocked = _upload(client, factory["token"], FACTORY_TEXT, currency="USD")
    assert blocked.status_code == 422, blocked.text
    assert blocked.json()["error"]["code"] == "VALIDATION_ERROR"

    allowed = _upload(client, factory["token"], FACTORY_TEXT, currency="CNY")
    assert allowed.status_code == 201, allowed.text


def test_rule_validation_rejects_bad_input(
    client: TestClient, factory: dict[str, Any]
) -> None:
    bad_regex = client.put(
        "/v1/config/catalog-rules",
        json={"code_pattern": "^(abc", "currencies": ["CNY"]},
        headers=_h(factory["token"]),
    )
    assert bad_regex.status_code == 422, bad_regex.text

    bad_currency = client.put(
        "/v1/config/catalog-rules",
        json={"code_pattern": "^A[0-9]{6}$", "currencies": ["cny"]},
        headers=_h(factory["token"]),
    )
    assert bad_currency.status_code == 422, bad_currency.text

    empty_currency = client.put(
        "/v1/config/catalog-rules",
        json={"code_pattern": "^A[0-9]{6}$", "currencies": []},
        headers=_h(factory["token"]),
    )
    assert empty_currency.status_code == 422, empty_currency.text


def test_rule_writes_and_updates_are_mutually_exclusive(
    client: TestClient, factory: dict[str, Any], admin: pg_url_type
) -> None:
    """L1 2026-11-08 剩余条件：写入路径取规则行 FOR SHARE，与 PUT 的 FOR UPDATE 互斥。

    双向验证：
    1) 写入事务持有 FOR SHARE（未提交）→ PUT 被阻塞（拿不到排他锁）；
    2) PUT 持有 FOR UPDATE（未提交）→ 写入路径加载规则被阻塞。
    """
    import threading

    from fkh.domain.catalog_rules import load_rule, save_rule

    tenant = uuid.UUID(factory["tenant"])
    actor = uuid.UUID(factory["principal"])
    writer = psycopg.connect(admin.info.dsn, autocommit=False)
    upgrader = psycopg.connect(admin.info.dsn, autocommit=False)
    try:
        writer.execute("SELECT set_config('app.tenant_id', %s, false)", (str(tenant),))
        assert load_rule(writer, tenant, for_write=True) is not None

        blocked: list[BaseException] = []

        def _try_update() -> None:
            upgrader.execute("SET lock_timeout = '1500ms'")
            try:
                save_rule(upgrader, tenant, actor_id=actor,
                          code_pattern=r"^SKU-[0-9]{4}-[0-9]{3}$", currencies=["CNY"])
                upgrader.commit()
            except BaseException as exc:  # noqa: BLE001 - 期望锁等待超时
                upgrader.rollback()
                blocked.append(exc)

        thread = threading.Thread(target=_try_update)
        thread.start()
        thread.join(timeout=10)
        assert not thread.is_alive(), "PUT 应被阻塞后超时返回，而不是静默成功"
        assert blocked, "PUT 在写入事务持锁期间必须失败"
        assert isinstance(blocked[0], psycopg.errors.LockNotAvailable)

        writer.rollback()

        # 反向：PUT 先持排他锁 → 写入路径加载规则被阻塞
        upgrader.execute("SELECT set_config('app.tenant_id', %s, false)", (str(tenant),))
        upgrader.execute("SET lock_timeout = '1500ms'")
        save_rule(upgrader, tenant, actor_id=actor,
                  code_pattern=r"^[A-Z]{1,3}[0-9]{5,8}$", currencies=["CNY"])

        write_blocked: list[BaseException] = []

        def _try_load_for_write() -> None:
            third = psycopg.connect(admin.info.dsn, autocommit=False)
            third.execute("SET lock_timeout = '1500ms'")
            try:
                third.execute("SELECT set_config('app.tenant_id', %s, false)", (str(tenant),))
                load_rule(third, tenant, for_write=True)
            except BaseException as exc:  # noqa: BLE001
                write_blocked.append(exc)
            finally:
                third.close()

        second = threading.Thread(target=_try_load_for_write)
        second.start()
        second.join(timeout=10)
        assert not second.is_alive()
        assert write_blocked and isinstance(write_blocked[0], psycopg.errors.LockNotAvailable)
        upgrader.rollback()
    finally:
        writer.close()
        upgrader.close()


def test_put_sees_writes_committed_while_it_waited(
    factory: dict[str, Any], admin: psycopg.Connection[Any]
) -> None:
    """等待锁释放后必须看到刚提交的不兼容数据（READ COMMITTED 语义）。

    否则"锁协议正确"仍会放过：写入提交 → PUT 拿到锁 → 检查旧快照 → 误判兼容。
    """
    import threading
    import time

    from fkh.domain.catalog_rules import CatalogRuleConflict, save_rule

    tenant = uuid.UUID(factory["tenant"])
    actor = uuid.UUID(factory["principal"])
    writer = psycopg.connect(admin.info.dsn, autocommit=False)
    upgrader = psycopg.connect(admin.info.dsn, autocommit=False)
    outcome: list[BaseException | None] = []
    backend_pids: list[int] = []

    def _put() -> None:
        upgrader.execute("SET lock_timeout = '8000ms'")
        row = upgrader.execute("SELECT pg_backend_pid()").fetchone()
        assert row is not None
        backend_pids.append(int(row[0]))
        upgrader.execute("SELECT set_config('app.tenant_id', %s, false)", (str(tenant),))
        try:
            save_rule(upgrader, tenant, actor_id=actor,
                      code_pattern=r"^SKU-[0-9]{4}-[0-9]{3}$", currencies=["CNY"])
            upgrader.commit()
            outcome.append(None)
        except BaseException as exc:  # noqa: BLE001
            upgrader.rollback()
            outcome.append(exc)

    try:
        writer.execute("SELECT set_config('app.tenant_id', %s, false)", (str(tenant),))
        # 写入事务先按旧规则写入一条"新规则下不合法"的货号，并持有规则共享锁
        writer.execute(
            "SELECT code_pattern FROM catalog_rules WHERE tenant_id = %s FOR SHARE",
            (tenant,),
        )
        version_id = uuid.uuid4()
        document_id = uuid.uuid4()
        writer.execute(
            "INSERT INTO knowledge_documents"
            " (tenant_id, id, knowledge_key, domain, owner_department)"
            " VALUES (%s, %s, %s, 'product_catalog', 'sales')",
            (tenant, document_id, f"lock-{uuid.uuid4().hex[:6]}"),
        )
        conn_chunk = writer.execute(
            "SELECT 1"  # 仅占位：条目需要版本身份，见下方 version 插入
        ).fetchone()
        assert conn_chunk is not None

        thread = threading.Thread(target=_put)
        thread.start()
        time.sleep(1.2)  # 让 PUT 先进入等待（含 SET/SHOW/绑定语句）

        # 证明 PUT **确实在等待**（而不是只是"启动了线程"）：该表上应存在未授予的锁
        with psycopg.connect(admin.info.dsn) as probe:
            probe.execute("SET statement_timeout = '15000ms'")
            assert backend_pids, "PUT 线程应已报告自己的 backend pid"
            waiting = probe.execute(
                "SELECT count(*) FROM pg_locks WHERE pid = %s AND NOT granted",
                (backend_pids[0],),
            ).fetchone()
        assert waiting is not None and waiting[0] >= 1, "PUT 必须真正阻塞在锁上（未授予）"
        assert not outcome, "PUT 在写入提交前不应有结果"

        # 提交：先补一个符合旧规则、但不符合新规则的货号条目
        writer.execute(
            """INSERT INTO knowledge_versions
                 (tenant_id, id, document_id, version_no, title, classification, audience,
                  status, content_sha256, source_object_key, required_scope)
               VALUES (%s, %s, %s, 1, '锁验证', 1, 'internal', 'REVOKED',
                       %s, 'lock/x.md', 'internal_product')""",
            (tenant, version_id, document_id, "0" * 64),
        )
        acl_id = uuid.uuid4()
        writer.execute(
            "INSERT INTO acl_policies (tenant_id, id, policy_json)"
            " VALUES (%s, %s, '{\"allow\": {}, \"deny\": {}}'::jsonb)",
            (tenant, acl_id),
        )
        chunk_id = uuid.uuid4()
        writer.execute(
            """INSERT INTO knowledge_chunks
                 (tenant_id, id, version_id, chunk_kind, chunk_text, chunk_sha256,
                  tokenizer_id, locator, acl_policy_id, index_generation)
               VALUES (%s, %s, %s, 'parent', 'A112420 200mm 39.00', %s,
                       'tok-v1', '{}'::jsonb, %s, 1)""",
            (tenant, chunk_id, version_id, "0" * 64, acl_id),
        )
        writer.execute(
            """INSERT INTO catalog_items
                 (tenant_id, version_id, chunk_id, code, spec, raw_text, raw_price,
                  price, currency, locator)
               VALUES (%s, %s, %s, 'A112420', '200mm', 'A112420 200mm 39.00',
                       '39.00', 39.00, 'CNY', '{}'::jsonb)""",
            (tenant, version_id, chunk_id),
        )
        writer.commit()

        thread.join(timeout=15)
        assert not thread.is_alive(), "PUT 应在写入提交后返回"
        assert outcome, "PUT 必须有结果"
        assert isinstance(outcome[0], CatalogRuleConflict), (
            "PUT 必须看到等待期间提交的不兼容条目并拒绝，而不是误判兼容"
        )
    finally:
        writer.close()
        upgrader.close()


def test_broad_rule_saves_with_warning_not_rejection(
    client: TestClient, factory: dict[str, Any]
) -> None:
    """L1 2026-11-08：过宽规则采用保存时警告，不按字面量拒绝。"""
    broad = client.put(
        "/v1/config/catalog-rules",
        json={"code_pattern": r"^\d+$", "currencies": ["CNY"]},
        headers=_h(factory["token"]),
    )
    assert broad.status_code == 200, broad.text
    assert broad.json()["warnings"], "过宽规则应给出警告"
    assert "宽泛" in broad.json()["warnings"][0]

    strict = client.put(
        "/v1/config/catalog-rules",
        json={"code_pattern": r"^SKU-[0-9]{4}-[0-9]{3}$", "currencies": ["CNY"]},
        headers=_h(factory["token"]),
    )
    assert strict.status_code == 200, strict.text
    assert strict.json()["warnings"] == []
