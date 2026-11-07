"""文档上传与列表：权限、体积、重复、可见性、端到端与原子性（2026-11-07 L1 裁决）。"""

from __future__ import annotations

import base64
import threading
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import jwt
import psycopg
import pytest
from fastapi.testclient import TestClient
from tests.conftest import create_principal, create_tenant

from fkh.api.main import create_app
from fkh.config import Settings

pytestmark = pytest.mark.requires_pg

SECRET = "document-api-secret-0123456789abcdef"
HASH_KEY = "document-hmac-key-0123456789"
HASH_KEY_ID = "k-doc"
ROOT = Path(__file__).resolve().parents[2]

CATALOG_TEXT = "第 1 页\n\n货号 规格 单价\nA112420 200mm 39.00\nA112430 300mm 48.00\n"
# 冲突判定按 child 块出现次数计：同一块内的重复行会被 parse_catalog_text 去重，
# 所以样例必须撑到两个 child 块（250–450 tokens/块），这正对应真实目录"同款跨页出现"的情形。
_FILLER = "冷凝管采用高硼硅玻璃制作，壁厚均匀，耐温差性能良好。" * 16
_HEAD = "货号 规格 管径*总长 上标口*下标口 单价"
CONFLICT_TEXT = (
    f"第 1 页\n\n{_HEAD}\nB900001 200mm 38*300 24*24 12.00\n{_FILLER}\n\n"
    f"第 2 页\n\n{_HEAD}\nB900001 200mm 38*300 24*24 13.00\n{_FILLER}\n"
)


def _token(
    tenant: str,
    principal: str,
    *,
    scopes: tuple[str, ...] = (
        "knowledge.ingest", "knowledge.read", "knowledge.review",
        "knowledge.search", "knowledge.publish",
    ),
    allowed: tuple[str, ...] = ("internal_product",),
) -> str:
    now = datetime.now(UTC)
    payload = {
        "iss": "fkh.local",
        "aud": "fkh-api",
        "sub": f"user-{principal[:8]}@example.test",
        "iat": now,
        "exp": now + timedelta(minutes=10),
        "tenant_id": tenant,
        "principal_id": principal,
        "roles": ["staff"],
        "scope": list(scopes),
        "allowed_scopes": list(allowed),
    }
    return jwt.encode(payload, SECRET, algorithm="HS256")


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
    tenant = create_tenant(admin, f"文档 {uuid.uuid4().hex[:6]}")
    principal = create_principal(admin, tenant, f"doc-{uuid.uuid4().hex[:6]}@example.test")
    admin.commit()
    return {"tenant": tenant, "principal": principal, "token": _token(tenant, principal)}


def _payload(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "filename": "catalog.md",
        "title": "目录",
        "domain": "product_catalog",
        "owner_department": "sales",
        "classification": 2,
        "audience": "internal",
        "required_scope": "internal_product",
        "acl_allow_roles": ["staff"],
        "knowledge_key": f"doc-{uuid.uuid4().hex[:8]}",
        "content_base64": base64.b64encode(CATALOG_TEXT.encode()).decode(),
        "catalog_currency": "CNY",
    }
    body.update(overrides)
    return body


def _upload(client: TestClient, token: str, **overrides: Any) -> Any:
    return client.post(
        "/v1/knowledge/documents",
        json=_payload(**overrides),
        headers={"Authorization": f"Bearer {token}"},
    )


def _list(client: TestClient, token: str, **params: Any) -> Any:
    return client.get(
        "/v1/knowledge/documents", params=params, headers={"Authorization": f"Bearer {token}"}
    )


def test_upload_creates_draft_and_is_not_searchable(
    client: TestClient, seeded: dict[str, str]
) -> None:
    response = _upload(client, seeded["token"], knowledge_key="k-upload-1")
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "DRAFT"
    assert body["child_chunks"] > 0

    listed = _list(client, seeded["token"], status="DRAFT").json()
    assert listed["total"] == 1
    assert listed["items"][0]["knowledge_key"] == "k-upload-1"
    assert listed["items"][0]["version_id"] == body["version_id"]

    # DRAFT 不可被检索（状态机决定，不是接口参数）
    search = client.post(
        "/v1/knowledge/search",
        json={"query": "A112420", "knowledge_scope": "internal_product"},
        headers={"Authorization": f"Bearer {seeded['token']}"},
    )
    assert search.status_code == 200
    assert search.json()["results"] == []


def test_upload_validation_and_limits(client: TestClient, seeded: dict[str, str]) -> None:
    bad_b64 = _upload(client, seeded["token"], content_base64="!!!not-base64!!!")
    assert bad_b64.status_code == 422

    empty = _upload(client, seeded["token"], content_base64=base64.b64encode(b"   ").decode())
    assert empty.status_code == 422

    bad_domain = _upload(client, seeded["token"], domain="unknown_domain")
    assert bad_domain.status_code == 422

    bad_class = _upload(client, seeded["token"], classification=9)
    assert bad_class.status_code == 422

    bad_scope = _upload(client, seeded["token"], required_scope="internal_training")
    assert bad_scope.status_code == 403, "数据范围授权：不在 allowed_scopes 内"

    acl_deny_self = _upload(client, seeded["token"], acl_allow_roles=["admin"])
    assert acl_deny_self.status_code == 403, "不能创建自己读不到的内容"


def test_exact_limit_ok_and_over_limit_rejected(
    client: TestClient, admin: psycopg.Connection[Any], seeded: dict[str, str]
) -> None:
    exact = b"a" * (5 * 1024 * 1024)
    ok = _upload(client, seeded["token"], content_base64=base64.b64encode(exact).decode())
    assert ok.status_code == 201, ok.text

    over = b"a" * (5 * 1024 * 1024 + 1)
    too_big = _upload(client, seeded["token"], content_base64=base64.b64encode(over).decode())
    assert too_big.status_code == 413

    admin.execute("SELECT set_config('app.tenant_id', %s, false)", (seeded["tenant"],))
    count = admin.execute(
        "SELECT count(*) FROM knowledge_documents WHERE tenant_id = %s", (seeded["tenant"],)
    ).fetchone()[0]
    assert count == 1, "超限请求不得写库"


def test_http_body_limit_is_enforced_before_parsing(
    client: TestClient, seeded: dict[str, str]
) -> None:
    huge = "a" * (9 * 1024 * 1024)
    response = client.post(
        "/v1/knowledge/documents",
        json=_payload(content_base64=huge),
        headers={"Authorization": f"Bearer {seeded['token']}"},
    )
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "PAYLOAD_TOO_LARGE"


def test_duplicate_key_and_concurrent_duplicate(
    client: TestClient, admin: psycopg.Connection[Any], seeded: dict[str, str]
) -> None:
    first = _upload(client, seeded["token"], knowledge_key="k-dup")
    assert first.status_code == 201
    second = _upload(client, seeded["token"], knowledge_key="k-dup")
    assert second.status_code == 409

    admin.execute("SELECT set_config('app.tenant_id', %s, false)", (seeded["tenant"],))
    docs = admin.execute(
        "SELECT count(*) FROM knowledge_documents WHERE tenant_id = %s AND knowledge_key = 'k-dup'",
        (seeded["tenant"],),
    ).fetchone()[0]
    assert docs == 1

    # 并发重复：唯一约束兜底，只能一次成功
    codes: list[int] = []
    lock = threading.Lock()

    def worker() -> None:
        r = _upload(client, seeded["token"], knowledge_key="k-race")
        with lock:
            codes.append(r.status_code)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(codes)[0] == 201 and set(codes) <= {201, 409}, codes

    versions = admin.execute(
        """SELECT count(*) FROM knowledge_versions v
             JOIN knowledge_documents d ON d.tenant_id = v.tenant_id AND d.id = v.document_id
            WHERE d.tenant_id = %s AND d.knowledge_key = 'k-race'""",
        (seeded["tenant"],),
    ).fetchone()[0]
    assert versions == 1, "并发重复不得产生第二个版本"


def test_scope_separation(client: TestClient, seeded: dict[str, str]) -> None:
    read_only = _token(
        seeded["tenant"], seeded["principal"], scopes=("knowledge.read",)
    )
    assert _list(client, read_only).status_code == 200, "只读管理员能看列表"

    denied_upload = _upload(client, read_only, knowledge_key="k-no-ingest")
    assert denied_upload.status_code == 403

    no_read = _token(
        seeded["tenant"], seeded["principal"], scopes=("knowledge.ingest",)
    )
    assert _list(client, no_read).status_code == 403

    uploaded = _upload(client, seeded["token"], knowledge_key="k-review-sep")
    version_id = uploaded.json()["version_id"]
    review = client.post(
        f"/v1/knowledge/{version_id}/review",
        json={"decision": "PASS"},
        headers={"Authorization": f"Bearer {read_only}"},
    )
    assert review.status_code == 403, "有列表权限不代表能审核"


def test_latest_version_invisible_removes_document(
    client: TestClient, admin: psycopg.Connection[Any], seeded: dict[str, str]
) -> None:
    """先取最新版本再过滤：最新版不可见时整条文档消失，不回退旧版本。"""
    first = _upload(client, seeded["token"], knowledge_key="k-versioned")
    version_id = first.json()["version_id"]
    reviewed = client.post(
        f"/v1/knowledge/{version_id}/review",
        json={"decision": "PASS"},
        headers={"Authorization": f"Bearer {seeded['token']}"},
    )
    assert reviewed.status_code == 200, reviewed.text
    assert _list(client, seeded["token"]).json()["total"] == 1

    # 同 key 再上传一份不同内容，但放到调用者看不到的 required_scope
    wider = _token(
        seeded["tenant"],
        seeded["principal"],
        allowed=("internal_product", "internal_training"),
    )
    second = _upload(
        client,
        wider,
        knowledge_key="k-versioned",
        required_scope="internal_training",
        content_base64=base64.b64encode("训练材料\n\n内容不同".encode()).decode(),
    )
    assert second.status_code == 201, second.text

    assert _list(client, seeded["token"]).json()["total"] == 0, "最新版本不可见 → 整条消失"


def test_list_filters_and_stable_order(client: TestClient, seeded: dict[str, str]) -> None:
    a = _upload(client, seeded["token"], knowledge_key="k-list-a")
    b = _upload(client, seeded["token"], knowledge_key="k-list-b")
    assert a.status_code == 201 and b.status_code == 201

    listed = _list(client, seeded["token"], limit=1).json()
    assert listed["total"] == 2 and len(listed["items"]) == 1
    second_page = _list(client, seeded["token"], limit=1, offset=1).json()
    assert second_page["items"][0]["document_id"] != listed["items"][0]["document_id"]

    filtered = _list(client, seeded["token"], domain="training").json()
    assert filtered["total"] == 0

    assert _list(client, seeded["token"], status="APPROVED_INTERNAL").json()["total"] == 0
    assert _list(client, seeded["token"], limit=0).status_code == 422


def test_upload_then_review_makes_searchable(client: TestClient, seeded: dict[str, str]) -> None:
    uploaded = _upload(client, seeded["token"], knowledge_key="k-e2e")
    version_id = uploaded.json()["version_id"]

    client.post(
        f"/v1/knowledge/{version_id}/review",
        json={"decision": "PASS"},
        headers={"Authorization": f"Bearer {seeded['token']}"},
    )
    published = client.post(
        f"/v1/knowledge/{version_id}/publish",
        json={"audience": "internal"},
        headers={"Authorization": f"Bearer {seeded['token']}"},
    )
    assert published.status_code == 200, published.text
    assert published.json()["status"] == "APPROVED_INTERNAL"

    search = client.post(
        "/v1/knowledge/search",
        json={"query": "A112420", "knowledge_scope": "internal_product"},
        headers={"Authorization": f"Bearer {seeded['token']}"},
    )
    assert search.status_code == 200
    assert search.json()["results"], "审批后可被检索"


def test_catalog_extraction_and_conflict_queue(
    client: TestClient, admin: psycopg.Connection[Any], seeded: dict[str, str]
) -> None:
    uploaded = _upload(client, seeded["token"], knowledge_key="k-catalog")
    assert uploaded.status_code == 201
    conflict = _upload(
        client,
        seeded["token"],
        knowledge_key="k-conflict",
        content_base64=base64.b64encode(CONFLICT_TEXT.encode()).decode(),
    )
    assert conflict.status_code == 201

    admin.execute("SELECT set_config('app.tenant_id', %s, false)", (seeded["tenant"],))
    items = admin.execute(
        "SELECT count(*) FROM catalog_items WHERE tenant_id = %s", (seeded["tenant"],)
    ).fetchone()[0]
    groups = admin.execute(
        "SELECT count(*) FROM catalog_conflict_groups WHERE tenant_id = %s", (seeded["tenant"],)
    ).fetchone()[0]
    assert items > 0, "product_catalog 上传应触发目录抽取"
    assert groups >= 1, "同货号两个价格应产生冲突待办"


def test_failure_midway_leaves_nothing(
    client: TestClient,
    admin: psycopg.Connection[Any],
    seeded: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """入库硬失败（切片阶段抛错）必须整体回滚：文档/版本/切片都不留。"""
    from fkh.workers.repository import IngestionRepository

    def boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("模拟切片写入失败")

    monkeypatch.setattr(IngestionRepository, "insert_chunks", boom)

    response = _upload(client, seeded["token"], knowledge_key="k-atomic")
    assert response.status_code == 500

    admin.execute("SELECT set_config('app.tenant_id', %s, false)", (seeded["tenant"],))
    docs = admin.execute(
        "SELECT count(*) FROM knowledge_documents WHERE tenant_id = %s", (seeded["tenant"],)
    ).fetchone()[0]
    versions = admin.execute(
        "SELECT count(*) FROM knowledge_versions WHERE tenant_id = %s", (seeded["tenant"],)
    ).fetchone()[0]
    chunks = admin.execute(
        "SELECT count(*) FROM knowledge_chunks WHERE tenant_id = %s", (seeded["tenant"],)
    ).fetchone()[0]
    assert (docs, versions, chunks) == (0, 0, 0)
