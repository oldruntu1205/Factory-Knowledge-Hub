"""正常返回 4xx 的错误响应必须回滚（不能靠"端点一定抛异常"）。"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

import jwt
import psycopg
import pytest
from fastapi import Depends
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from tests.conftest import create_principal, create_tenant

from fkh.api.db import get_conn
from fkh.api.main import create_app
from fkh.api.security import PrincipalDep, require
from fkh.config import Settings

pytestmark = pytest.mark.requires_pg

SECRET = "commit-boundary-secret-0123456789"
HASH_KEY = "commit-boundary-hmac-0123456789"
HASH_KEY_ID = "k-commit"


def _token(tenant: str, principal: str) -> str:
    now = datetime.now(UTC)
    return jwt.encode(
        {
            "iss": "fkh.local", "aud": "fkh-api", "sub": f"user-{principal[:8]}@test.local",
            "iat": now, "exp": now + timedelta(minutes=10),
            "tenant_id": tenant, "principal_id": principal,
            "roles": ["staff"], "scope": ["knowledge.write"],
            "allowed_scopes": ["internal_product"],
        },
        SECRET,
        algorithm="HS256",
    )


@pytest.fixture
def client(pg_url: str) -> Iterator[TestClient]:
    settings = Settings(
        jwt_secret=SECRET,
        database_url=pg_url,
        retrieval_hash_key=HASH_KEY,
        retrieval_hash_key_id=HASH_KEY_ID,
    )
    with TestClient(create_app(settings), raise_server_exceptions=False) as test_client:
        yield test_client


@pytest.fixture
def seeded(admin: psycopg.Connection[Any]) -> dict[str, str]:
    tenant = create_tenant(admin, f"提交边界 {uuid.uuid4().hex[:6]}")
    principal = create_principal(admin, tenant, f"commit-{uuid.uuid4().hex[:6]}@test.local")
    admin.commit()
    return {"tenant": tenant, "principal": principal}


def test_normal_4xx_response_rolls_back_written_rows(
    client: TestClient, admin: psycopg.Connection[Any], seeded: dict[str, str]
) -> None:
    code = f"rollback_{uuid.uuid4().hex[:8]}"

    @client.app.post(
        "/__test/normal-4xx", dependencies=[Depends(require("knowledge.write"))]
    )
    def _normal_4xx(
        context: PrincipalDep, conn: Annotated[psycopg.Connection[Any], Depends(get_conn)]
    ) -> JSONResponse:
        conn.execute(
            "INSERT INTO knowledge_domains (tenant_id, code, label, is_default)"
            " VALUES (%s, %s, %s, false)",
            (context.tenant_id, code, "commit-boundary"),
        )
        # 正常 return 一个 4xx（不是 raise）：yield 依赖会正常结束。
        return JSONResponse(status_code=409, content={"error": {"code": "CONFLICT"}})

    response = client.post(
        "/__test/normal-4xx",
        headers={"Authorization": f"Bearer {_token(seeded['tenant'], seeded['principal'])}"},
    )
    assert response.status_code == 409

    row = admin.execute(
        "SELECT 1 FROM knowledge_domains WHERE tenant_id = %s AND code = %s",
        (seeded["tenant"], code),
    ).fetchone()
    assert row is None, "正常返回 4xx 时写入必须已回滚"
