"""业务角色（非 owner、无 BYPASSRLS）测试辅助。

2026-11-07 起约定：迁移/造数用管理员连接，**API 必须用业务角色**，
否则 owner 身份绕过 RLS，测出来的隔离是假的。
"""

from __future__ import annotations

import uuid
from urllib.parse import urlparse

import psycopg


def role_dsn(pg_url: str, role: str) -> str:
    parsed = urlparse(pg_url)
    return f"postgresql://{role}@{parsed.hostname}:{parsed.port or 5432}{parsed.path}"


def ensure_business_role(pg_url: str, *, prefix: str = "fkh_app") -> tuple[str, str]:
    """建一个业务角色并授予 DML 权限；返回 (role_name, dsn)。"""
    role = f"{prefix}_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(pg_url, autocommit=True) as admin:
        admin.execute(f"CREATE ROLE {role} LOGIN")
        admin.execute(f"GRANT USAGE ON SCHEMA public TO {role}")
        admin.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {role}")
        admin.execute(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {role}")
        admin.execute(f"GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA public TO {role}")
        flags = admin.execute(
            "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = %s", (role,)
        ).fetchone()
    assert flags == (False, False), "业务角色必须既不是 superuser 也不是 BYPASSRLS"
    return role, role_dsn(pg_url, role)


def drop_business_role(pg_url: str, role: str) -> None:
    with psycopg.connect(pg_url, autocommit=True) as admin:
        admin.execute(f"DROP OWNED BY {role}")
        admin.execute(f"DROP ROLE IF EXISTS {role}")
