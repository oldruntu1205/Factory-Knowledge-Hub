"""pytest 公共装置。

数据库来源优先级：
1. FKH_TEST_DATABASE_URL —— CI 中的 pgvector service 容器；
2. 本机 pgserver 内嵌 PostgreSQL —— 用于无 Docker 环境的本地验证；
3. 两者皆无 —— 标记 requires_pg 的测试显式跳过，并在摘要中报告。

测试隔离策略：
- schema 由 migrations/apply.py 全量构建，测的是真实迁移产物，不是手工建表；
- 造数连接使用超级用户，业务断言连接通过 SET ROLE 降级为无 BYPASSRLS 的 fkh_app，
  这样 RLS 才会真正生效（超级用户会绕过策略，用超级用户测 RLS 等于没测）。
"""

from __future__ import annotations

import importlib.util
import os
import tempfile
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest

ROOT = Path(__file__).resolve().parents[1]
APP_ROLE = "fkh_app"


def _load_apply_module() -> Any:
    spec = importlib.util.spec_from_file_location("fkh_migrations_apply", ROOT / "migrations" / "apply.py")
    if spec is None or spec.loader is None:
        raise RuntimeError("无法加载 migrations/apply.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _bootstrap(url: str) -> None:
    _load_apply_module().main(["--url", url])


@pytest.fixture(scope="session")
def pg_url() -> str:
    external = os.environ.get("FKH_TEST_DATABASE_URL")
    if external:
        _bootstrap(external)
        return external

    try:
        import pgserver
    except ImportError:  # pragma: no cover - 取决于环境
        pytest.skip("requires_pg: 未配置 FKH_TEST_DATABASE_URL，且本机未安装 pgserver")

    # 数据目录默认放系统临时目录，不放仓库内。
    # 工作区里的 pgdata 会被文件监控/索引/杀毒实时扫描，PostgreSQL 在启动时
    # 写 pgdata/log 会撞上 sharing violation，卡住 30 秒重试而 pg_ctl 只等 10 秒，
    # 表现为"所有 requires_pg 测试 fixture 报错"。
    data_dir = os.environ.get("FKH_TEST_PGDATA") or os.path.join(
        tempfile.gettempdir(), "fkh-pgdata"
    )
    os.makedirs(data_dir, exist_ok=True)
    server = pgserver.get_server(data_dir)
    uri = str(server.get_uri())
    _bootstrap(uri)
    return uri


@pytest.fixture(scope="session")
def app_role(pg_url: str) -> str:
    """创建一个无超级权限、无 BYPASSRLS 的业务角色，并授予 DML 权限。

    用 CREATE IF NOT EXISTS + ALTER 保证重复执行幂等：直接 DROP ROLE 会因角色持有
    对象权限而失败，进而导致 CREATE 撞 DuplicateObject。
    """
    with psycopg.connect(pg_url, autocommit=True) as conn:
        conn.execute(
            f"""DO $$
                BEGIN
                  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{APP_ROLE}') THEN
                    CREATE ROLE {APP_ROLE} LOGIN;
                  END IF;
                END
                $$;"""
        )
        conn.execute(f"ALTER ROLE {APP_ROLE} NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE LOGIN")
        conn.execute(f"GRANT USAGE ON SCHEMA public TO {APP_ROLE}")
        conn.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {APP_ROLE}")
    return APP_ROLE


@pytest.fixture
def admin(pg_url: str) -> Iterator[psycopg.Connection]:
    """超级用户连接：造数与结构断言。默认不开自动提交，异常后由 rollback 清理。"""
    with psycopg.connect(pg_url, autocommit=False) as conn:
        yield conn
        conn.rollback()


@pytest.fixture
def app_conn(pg_url: str, app_role: str) -> Iterator[psycopg.Connection]:
    """业务角色连接：RLS 在此连接上真实生效。"""
    with psycopg.connect(pg_url, autocommit=False) as conn:
        conn.execute(f"SET ROLE {app_role}")
        yield conn
        conn.rollback()


@pytest.fixture(scope="session")
def relay_role(pg_url: str) -> str:
    """创建 outbox relay 的平台角色。

    角色 DDL 的单一来源是 `deploy/sql/relay_role.sql`：迁移只管 schema，
    角色是集群级对象，回滚迁移不应顺手删角色。
    """
    sql = (ROOT / "deploy" / "sql" / "relay_role.sql").read_text(encoding="utf-8")
    with psycopg.connect(pg_url, autocommit=True) as conn:
        conn.execute(sql)
    return "fkh_relay"


@pytest.fixture
def relay_conn(pg_url: str, relay_role: str) -> Iterator[psycopg.Connection]:
    """以平台角色（BYPASSRLS）建立的连接：跨租户可见，但只能改投递状态列。"""
    with psycopg.connect(pg_url, autocommit=False) as conn:
        conn.execute(f"SET ROLE {relay_role}")
        yield conn
        conn.rollback()


# ---------------------------------------------------------------------------
# 造数工具
# ---------------------------------------------------------------------------
def sha256_hex(seed: str) -> str:
    import hashlib

    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def set_tenant(conn: psycopg.Connection, tenant_id: str) -> None:
    """事务级设置租户上下文。

    必须用 set_config 而非 SET LOCAL：SET 语句不支持参数绑定，
    拼接字符串会引入注入面，且 UUID 校验会退化为字符串比较。
    """
    conn.execute("SELECT set_config('app.tenant_id', %s, true)", (tenant_id,))


def set_principal(conn: psycopg.Connection, principal_id: str) -> None:
    conn.execute("SELECT set_config('app.principal_id', %s, true)", (principal_id,))


def create_tenant(conn: psycopg.Connection, name: str) -> str:
    row = conn.execute("INSERT INTO tenants (name) VALUES (%s) RETURNING id", (name,)).fetchone()
    assert row is not None
    return str(row[0])


def create_principal(conn: psycopg.Connection, tenant_id: str, subject: str, kind: str = "user") -> str:
    row = conn.execute(
        "INSERT INTO principals (tenant_id, external_subject, principal_type) VALUES (%s, %s, %s) RETURNING id",
        (tenant_id, subject, kind),
    ).fetchone()
    assert row is not None
    return str(row[0])


def create_document(conn: psycopg.Connection, tenant_id: str, key: str) -> str:
    row = conn.execute(
        """INSERT INTO knowledge_documents (tenant_id, knowledge_key, domain, owner_department)
           VALUES (%s, %s, 'product_catalog', 'tech') RETURNING id""",
        (tenant_id, key),
    ).fetchone()
    assert row is not None
    return str(row[0])


def create_version(
    conn: psycopg.Connection,
    tenant_id: str,
    document_id: str,
    *,
    status: str = "DRAFT",
    version_no: int = 1,
    classification: int = 1,
    audience: str = "internal",
    approved_by: str | None = None,
    public_approved_by: str | None = None,
) -> str:
    row = conn.execute(
        """INSERT INTO knowledge_versions
             (tenant_id, document_id, version_no, title, classification, audience, status,
              content_sha256, source_object_key, approved_by, public_approved_by)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
           RETURNING id""",
        (
            tenant_id,
            document_id,
            version_no,
            f"demo-{uuid.uuid4().hex[:8]}",
            classification,
            audience,
            status,
            sha256_hex(f"{document_id}-{version_no}"),
            f"s3://bucket/{document_id}/{version_no}.pdf",
            approved_by,
            public_approved_by,
        ),
    ).fetchone()
    assert row is not None
    return str(row[0])


def create_acl(conn: psycopg.Connection, tenant_id: str, policy: dict[str, Any] | None = None) -> str:
    row = conn.execute(
        "INSERT INTO acl_policies (tenant_id, policy_json) VALUES (%s, %s) RETURNING id",
        (tenant_id, psycopg.types.json.Jsonb(policy or {"allow": {"roles": ["staff"]}, "deny": {}})),
    ).fetchone()
    assert row is not None
    return str(row[0])


def create_chunk(
    conn: psycopg.Connection,
    tenant_id: str,
    version_id: str,
    acl_id: str,
    *,
    kind: str = "parent",
    parent_id: str | None = None,
    text: str = "示例内容",
) -> str:
    row = conn.execute(
        """INSERT INTO knowledge_chunks
             (tenant_id, version_id, parent_chunk_id, chunk_kind, chunk_text, chunk_sha256,
              tokenizer_id, locator, acl_policy_id, index_generation)
           VALUES (%s, %s, %s, %s, %s, %s, 'bge-m3-zh-v1', '{"page": 1}'::jsonb, %s, 1)
           RETURNING id""",
        (tenant_id, version_id, parent_id, kind, text, sha256_hex(text), acl_id),
    ).fetchone()
    assert row is not None
    return str(row[0])


@pytest.fixture(scope="session")
def seed(pg_url: str) -> dict[str, str]:
    """两个租户的最小数据集，供隔离性测试共享（只读）。"""
    with psycopg.connect(pg_url, autocommit=True) as conn:
        tenant_a = create_tenant(conn, "租户 A · 玻璃仪器厂")
        tenant_b = create_tenant(conn, "租户 B · 对照租户")
        user_a = create_principal(conn, tenant_a, "user-a@example.test")
        user_b = create_principal(conn, tenant_b, "user-b@example.test")
        doc_a = create_document(conn, tenant_a, "catalog-condenser")
        doc_b = create_document(conn, tenant_b, "catalog-beaker")
        acl_a = create_acl(conn, tenant_a)
        version_a = create_version(conn, tenant_a, doc_a, status="APPROVED_INTERNAL", approved_by=user_a)

    return {
        "tenant_a": tenant_a,
        "tenant_b": tenant_b,
        "user_a": user_a,
        "user_b": user_b,
        "doc_a": doc_a,
        "doc_b": doc_b,
        "acl_a": acl_a,
        "version_a": version_a,
    }
