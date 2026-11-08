"""文档上传与列表（2026-11-07 L1 裁决，业务前端「文档审核页」前置）。

权限分层（裁决 1）：
- 上传 `knowledge.ingest`；列表 `knowledge.read`（**文档管理元数据**读取）；
- 审核沿用 `knowledge.review`；三者互不隐含。
- 这些操作权限与**数据范围授权**（`required_scope` 是否在令牌 allowed_scopes 内）分别校验。

安全约束（裁决 2、3、4）：
- JSON + base64，解码后 ≤ 5 MiB；HTTP 请求体另有上限（中间件拦，见 main.py）；
- 体积/格式/字段/权限全部在写库前校验；
- `knowledge_key` 重复由数据库唯一约束兜底 → 409，失败不留半成品；
- 不能指定初始状态：入库固定 DRAFT，必须走既有多步审批才可被检索；
- 上传者必须是"自己也能读到"这份内容：请求里的 ACL 对调用者不通过时拒绝。
"""

from __future__ import annotations

import base64
import binascii
from typing import Annotated, Any, Literal

import psycopg
from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from fkh.api.db import get_conn
from fkh.api.errors import ApiError, ErrorCode
from fkh.api.security import PrincipalDep
from fkh.authz.policy import AclPolicy, Decision, evaluate
from fkh.catalog.conflicts import bind_tenant
from fkh.domain.catalog_rules import CatalogRuleMissing
from fkh.domain.domains import DomainError, DomainMissing, DomainService
from fkh.workers.ingestion import IngestionRequest, ingest_document
from fkh.workers.repository import DuplicateContentError

router = APIRouter(tags=["knowledge"])

ConnDep = Annotated[psycopg.Connection[Any], Depends(get_conn)]

MAX_DECODED_BYTES = 5 * 1024 * 1024
MAX_TITLE = 200
MAX_KEY = 120
MAX_FILENAME = 200
MAX_DEPARTMENT = 100
MAX_ACL_ROLES = 20
MAX_LIMIT = 200
DEFAULT_LIMIT = 50

VALID_DOMAINS = (
    "owner_experience",
    "glass_materials",
    "production_process",
    "product_catalog",
    "quality",
    "equipment_safety",
    "foreign_trade",
    "training",
)
VALID_SCOPES = ("internal_product", "internal_training", "customer_public")
VALID_STATUSES = (
    "RAW",
    "DRAFT",
    "REVIEWED",
    "APPROVED_INTERNAL",
    "PUBLISHED_PUBLIC",
    "SUPERSEDED",
    "REVOKED",
    "DISCARDED",
)

_LATEST_SQL = """
WITH latest AS (
  SELECT DISTINCT ON (document_id)
         tenant_id, document_id, id AS version_id, version_no, title, status,
         classification, audience, required_scope, created_at
    FROM knowledge_versions
   WHERE tenant_id = %(tenant)s
   ORDER BY document_id, version_no DESC
)
SELECT d.id, d.knowledge_key, d.domain, d.owner_department,
       l.version_id, l.version_no, l.title, l.status, l.classification, l.audience,
       l.required_scope, l.created_at,
       (SELECT count(*) FROM knowledge_chunks c
         WHERE c.tenant_id = l.tenant_id AND c.version_id = l.version_id) AS chunk_count
  FROM latest l
  JOIN knowledge_documents d
    ON d.tenant_id = l.tenant_id AND d.id = l.document_id
 WHERE l.required_scope = ANY(%(scopes)s)
   {extra}
 ORDER BY l.created_at DESC, d.id ASC
 LIMIT %(limit)s OFFSET %(offset)s
"""

_COUNT_SQL = """
WITH latest AS (
  SELECT DISTINCT ON (document_id)
         tenant_id, document_id, status, required_scope
    FROM knowledge_versions
   WHERE tenant_id = %(tenant)s
   ORDER BY document_id, version_no DESC
)
SELECT count(*)
  FROM latest l
  JOIN knowledge_documents d
    ON d.tenant_id = l.tenant_id AND d.id = l.document_id
 WHERE l.required_scope = ANY(%(scopes)s)
   {extra}
"""


class DocumentUpload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    filename: str = Field(min_length=1, max_length=MAX_FILENAME)
    title: str = Field(min_length=1, max_length=MAX_TITLE)
    domain: str
    owner_department: str = Field(min_length=1, max_length=MAX_DEPARTMENT)
    classification: int = Field(default=2, ge=0, le=4)
    audience: Literal["internal", "customer_public"] = "internal"
    required_scope: Literal["internal_product", "internal_training", "customer_public"]
    acl_allow_roles: list[str] = Field(default_factory=lambda: ["staff"], max_length=MAX_ACL_ROLES)
    acl_deny_roles: list[str] = Field(default_factory=list, max_length=MAX_ACL_ROLES)
    knowledge_key: str = Field(min_length=1, max_length=MAX_KEY)
    content_base64: str
    catalog_currency: str | None = None
    language: str = "zh-CN"


def _decode(content_base64: str) -> bytes:
    try:
        raw = base64.b64decode(content_base64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ApiError(
            ErrorCode.VALIDATION_ERROR,
            detail="content_base64 不是合法 base64",
            hint="content_base64: must be valid base64",
        ) from exc
    if len(raw) > MAX_DECODED_BYTES:
        raise ApiError(
            ErrorCode.PAYLOAD_TOO_LARGE,
            detail=f"解码后 {len(raw)} 字节，超过上限 {MAX_DECODED_BYTES}",
            hint=f"content_base64: decoded size must be <= {MAX_DECODED_BYTES} bytes",
        )
    if not raw.strip():
        raise ApiError(
            ErrorCode.VALIDATION_ERROR,
            detail="正文为空",
            hint="content_base64: decoded body must not be empty",
        )
    return raw


def _assembled_acl(payload: DocumentUpload) -> AclPolicy:
    return AclPolicy.from_json(
        {"allow": {"roles": payload.acl_allow_roles}, "deny": {"roles": payload.acl_deny_roles}}
    )


@router.post("/v1/knowledge/documents", status_code=201, summary="上传文档并入库（初始为 DRAFT）")
def upload_document(
    payload: DocumentUpload, context: PrincipalDep, conn: ConnDep
) -> dict[str, Any]:
    context.require_scope("knowledge.ingest")

    # 2026-11-08（ADR-009）：域改为实例配置，校验本实例清单而不是硬编码枚举
    try:
        DomainService(conn, context.tenant_id).assert_usable(payload.domain)
    except DomainMissing as exc:
        raise ApiError(
            ErrorCode.VALIDATION_ERROR,
            detail=str(exc),
            hint="domain: register it via POST /v1/config/domains first",
        ) from exc
    except DomainError as exc:
        raise ApiError(
            ErrorCode.VALIDATION_ERROR,
            detail=str(exc),
            hint="domain: lowercase letters/digits/underscore, starting with a letter",
        ) from exc
    if payload.required_scope not in context.allowed_scopes:
        # 数据范围授权：不能上传到自己的授权范围之外
        raise ApiError(
            ErrorCode.ACCESS_DENIED,
            detail=f"required_scope={payload.required_scope} 超出授权范围",
        )

    acl = _assembled_acl(payload)
    decision, reason = evaluate(
        acl, context.to_subject(), resource_tenant_id=str(context.tenant_id)
    )
    if decision is not Decision.ALLOW:
        # 不能创建"自己都读不到"的内容
        raise ApiError(ErrorCode.ACCESS_DENIED, detail=f"请求的 ACL 拒绝上传者本人：{reason}")

    content = _decode(payload.content_base64)
    bind_tenant(conn, context.tenant_id)

    try:
        with conn.transaction():
            result = ingest_document(
                conn,
                context.tenant_id,
                IngestionRequest(
                    source_id=f"upload-{payload.knowledge_key[:40]}",
                    filename=payload.filename,
                    content=content,
                    knowledge_key=payload.knowledge_key,
                    title=payload.title,
                    domain=payload.domain,
                    owner_department=payload.owner_department,
                    classification=payload.classification,
                    audience=payload.audience,
                    required_scope=payload.required_scope,
                    acl_policy=acl,
                    actor_id=context.principal_id,
                    language=payload.language,
                    catalog_currency=payload.catalog_currency,
                ),
            )
    except psycopg.errors.UniqueViolation as exc:
        # 唯一约束兜底：并发重复提交也只有一次成功
        raise ApiError(
            ErrorCode.VERSION_CONFLICT, detail=f"knowledge_key 已存在：{payload.knowledge_key}"
        ) from exc
    except DuplicateContentError as exc:
        # 同一文档下内容完全相同的版本：同样是"重复拒绝"，不静默再建一版
        raise ApiError(ErrorCode.VERSION_CONFLICT, detail=str(exc)) from exc
    except CatalogRuleMissing as exc:
        raise ApiError(
            ErrorCode.VALIDATION_ERROR,
            detail=str(exc),
            hint="catalog-rules: configure via PUT /v1/config/catalog-rules first",
        ) from exc
    except ValueError as exc:
        raise ApiError(ErrorCode.VALIDATION_ERROR, detail=str(exc)) from exc

    return {
        "document_id": str(result.document_id),
        "version_id": str(result.version_id),
        "version_no": result.version_no,
        "status": result.status.value,
        "parent_chunks": result.parent_count,
        "child_chunks": result.child_count,
        "needs_human_review": result.needs_human_review,
        "warnings": list(result.warnings),
    }


@router.get("/v1/knowledge/documents", summary="文档列表（审核用，含未检索状态）")
def list_documents(
    context: PrincipalDep,
    conn: ConnDep,
    status: str | None = None,
    domain: str | None = None,
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
) -> dict[str, Any]:
    context.require_scope("knowledge.read")
    if status is not None and status not in VALID_STATUSES:
        raise ApiError(
            ErrorCode.VALIDATION_ERROR,
            detail=f"status 必须是 {VALID_STATUSES} 之一",
            hint="status: not one of the allowed values",
        )
    if domain is not None and domain not in VALID_DOMAINS:
        raise ApiError(
            ErrorCode.VALIDATION_ERROR,
            detail=f"domain 必须是 {VALID_DOMAINS} 之一",
            hint="domain: not one of the allowed values",
        )
    if not 1 <= limit <= MAX_LIMIT:
        raise ApiError(
            ErrorCode.VALIDATION_ERROR,
            detail=f"limit 必须在 1–{MAX_LIMIT}",
            hint=f"limit: must be between 1 and {MAX_LIMIT}",
        )
    if offset < 0:
        raise ApiError(
            ErrorCode.VALIDATION_ERROR,
            detail="offset 不能为负",
            hint="offset: must be >= 0",
        )

    bind_tenant(conn, context.tenant_id)
    params: dict[str, Any] = {
        "tenant": context.tenant_id,
        "scopes": sorted(context.allowed_scopes),
        "limit": limit,
        "offset": offset,
    }
    extra = ""
    if status is not None:
        extra += " AND l.status = %(status)s"
        params["status"] = status
    if domain is not None:
        extra += " AND d.domain = %(domain)s"
        params["domain"] = domain

    # 「先取最新版本，再按范围/状态过滤，最后排序分页」：最新版本不可见时整条文档消失，
    # 不回退到旧版本（否则审核页会展示一个已不该被看到的历史版本）。
    rows = conn.execute(_LATEST_SQL.format(extra=extra), params).fetchall()
    total_row = conn.execute(_COUNT_SQL.format(extra=extra), params).fetchone()
    total = int(total_row[0]) if total_row else 0

    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "items": [
            {
                "document_id": str(row[0]),
                "knowledge_key": str(row[1]),
                "domain": str(row[2]),
                "owner_department": str(row[3]),
                "version_id": str(row[4]),
                "version_no": int(row[5]),
                "title": str(row[6]),
                "status": str(row[7]),
                "classification": int(row[8]),
                "audience": str(row[9]),
                "required_scope": str(row[10]),
                "created_at": row[11].isoformat() if row[11] is not None else None,
                "chunk_count": int(row[12]),
            }
            for row in rows
        ],
    }
