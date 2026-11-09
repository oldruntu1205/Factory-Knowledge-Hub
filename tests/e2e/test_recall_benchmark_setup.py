"""评测实例的种子配置：0035/0036 后缺 domain/rule 会让入库直接 FK 失败。"""

from __future__ import annotations

from typing import Any

import psycopg
import pytest

from fkh.tools.recall_benchmark import _seed_tenant

pytestmark = pytest.mark.requires_pg


def test_seed_tenant_creates_domain_and_catalog_rule(pg_url: str) -> None:
    with psycopg.connect(pg_url) as conn:
        tenant, principal = _seed_tenant(conn)

    with psycopg.connect(pg_url) as check:
        domain = check.execute(
            "SELECT 1 FROM knowledge_domains WHERE tenant_id = %s AND code = 'product_catalog'",
            (tenant,),
        ).fetchone()
        rule = check.execute(
            "SELECT code_pattern, currencies FROM catalog_rules WHERE tenant_id = %s",
            (tenant,),
        ).fetchone()
        principal_row = check.execute(
            "SELECT 1 FROM principals WHERE tenant_id = %s AND id = %s", (tenant, principal)
        ).fetchone()

    assert domain is not None
    assert principal_row is not None
    assert rule is not None
    pattern, currencies = rule
    assert pattern == "^[A-Z]{1,3}[0-9]{5,8}$"
    assert list(currencies) == ["CNY"]


def test_seed_tenant_is_idempotent_per_tenant(pg_url: str) -> None:
    """两次调用创建两个独立租户，互不覆盖（不是复用同一个 domain）。"""

    tenants: list[Any] = []
    with psycopg.connect(pg_url) as conn:
        for _ in range(2):
            tenant, _principal = _seed_tenant(conn)
            tenants.append(tenant)

    with psycopg.connect(pg_url) as check:
        rows = check.execute(
            "SELECT count(*) FROM knowledge_domains WHERE tenant_id = ANY(%s)",
            (tenants,),
        ).fetchone()

    assert rows is not None and int(rows[0]) == 2
