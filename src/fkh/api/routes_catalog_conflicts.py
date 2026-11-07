"""目录冲突待办的读取与决议（2026-11-07 L1 裁决）。

权限：
- 读取 `catalog.read`；决议再加 `catalog.resolve`；
- 决议的身份取自令牌，服务端用 `require_operator` 复核「该租户、活跃、user 主体」，不信任签发时的状态。

可见性（裁决 2）：先按租户/版本可操作状态/required_scope/ACL 过滤，**再计数、再分页**。
资源级拒绝一律 404；调用资格不足（缺 scope / 非 user / 已停用）403；
两者都通过、但版本已撤销/弃用/被替代 → 409。

响应不含原文整行（裁决 5）：只给 spec / raw_price / locator。
"""

from __future__ import annotations

from typing import Annotated, Any, Literal
from uuid import UUID

import psycopg
from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from fkh.api.db import get_conn
from fkh.api.errors import ApiError, ErrorCode
from fkh.api.security import PrincipalDep
from fkh.authz.policy import AclPolicy, Decision, evaluate
from fkh.catalog.conflicts import (
    OPERABLE_STATUSES,
    ConflictError,
    bind_tenant,
    get_group,
    require_operator,
    resolve_group,
)

router = APIRouter(tags=["catalog"])

ConnDep = Annotated[psycopg.Connection[Any], Depends(get_conn)]

MAX_LIMIT = 200
DEFAULT_LIMIT = 50
MAX_NOTE = 500
_STATUSES = ("OPEN", "KEEP", "DROP_ALL")

# 注意：ACL 挂在 chunk 上，版本表没有 acl_policy_id；组的可见性由它所有候选 chunk 的 ACL 决定。
_GROUP_SQL = """
SELECT g.group_id, g.version_id, g.code, g.status, g.note, g.resolved_by, g.resolved_at,
       g.selected_candidate_id, v.status AS version_status, v.required_scope
  FROM catalog_conflict_groups g
  JOIN knowledge_versions v
    ON v.tenant_id = g.tenant_id AND v.id = g.version_id
 WHERE g.tenant_id = %s
"""

_CANDIDATE_POLICY_SQL = """
SELECT c.group_id, p.policy_json
  FROM catalog_conflict_candidates c
  JOIN knowledge_chunks ch
    ON ch.tenant_id = c.tenant_id AND ch.id = c.chunk_id
  LEFT JOIN acl_policies p
    ON p.tenant_id = ch.tenant_id AND p.id = ch.acl_policy_id
 WHERE c.tenant_id = %s
"""


class ResolveBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["KEEP", "DROP_ALL"]
    candidate_id: UUID | None = None
    note: str | None = Field(default=None, max_length=MAX_NOTE)


def _policies_by_group(
    conn: psycopg.Connection[Any], tenant_id: UUID, group_ids: list[UUID]
) -> dict[str, list[Any]]:
    if not group_ids:
        return {}
    rows = conn.execute(
        _CANDIDATE_POLICY_SQL + " AND c.group_id = ANY(%s)",
        (tenant_id, group_ids),
    ).fetchall()
    result: dict[str, list[Any]] = {}
    for row in rows:
        result.setdefault(str(row[0]), []).append(row[1])
    return result


def _visible(
    context: Any, required_scope: str, policies: list[Any]
) -> bool:
    """资源级可见性：required_scope 在授权范围内，且**该组所有候选 chunk 的 ACL 都允许**。

    只要有一条候选被拒，就把整组藏起来——否则复核人员会看到"缺了一条价格"的对照表，
    据此做决议等于在残缺证据上下结论。
    """
    if required_scope not in context.allowed_scopes:
        return False
    if not policies:
        return False
    for raw in policies:
        policy = AclPolicy.from_json(raw) if raw is not None else None
        decision, _ = evaluate(
            policy, context.to_subject(), resource_tenant_id=str(context.tenant_id)
        )
        if decision is not Decision.ALLOW:
            return False
    return True


def _candidates(
    conn: psycopg.Connection[Any], tenant_id: UUID, group_ids: list[UUID]
) -> dict[str, list[dict[str, Any]]]:
    if not group_ids:
        return {}
    rows = conn.execute(
        """SELECT group_id, candidate_id, chunk_id, spec, raw_price, locator
             FROM catalog_conflict_candidates
            WHERE tenant_id = %s AND group_id = ANY(%s)
            ORDER BY created_at, candidate_id""",
        (tenant_id, group_ids),
    ).fetchall()
    result: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        result.setdefault(str(row[0]), []).append(
            {
                "candidate_id": str(row[1]),
                "chunk_id": str(row[2]),
                "spec": str(row[3]),
                "raw_price": str(row[4]),
                "locator": dict(row[5]) if row[5] else {},
            }
        )
    return result


def _group_view(row: Any, candidates: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "group_id": str(row[0]),
        "version_id": str(row[1]),
        "code": str(row[2]),
        "status": str(row[3]),
        "note": row[4],
        "resolved_by": str(row[5]) if row[5] else None,
        "resolved_at": row[6].isoformat() if row[6] is not None else None,
        "selected_candidate_id": str(row[7]) if row[7] else None,
        "candidates": candidates,
    }


@router.get("/v1/catalog/conflicts", summary="目录冲突待办列表")
def list_conflicts(
    context: PrincipalDep,
    conn: ConnDep,
    status: str = "OPEN",
    code: str | None = None,
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
) -> dict[str, Any]:
    context.require_scope("catalog.read")
    if status not in _STATUSES:
        raise ApiError(ErrorCode.VALIDATION_ERROR, detail=f"status 只能是 {_STATUSES}")
    if not 1 <= limit <= MAX_LIMIT:
        raise ApiError(ErrorCode.VALIDATION_ERROR, detail=f"limit 必须在 1–{MAX_LIMIT}")
    if offset < 0:
        raise ApiError(ErrorCode.VALIDATION_ERROR, detail="offset 不能为负")

    bind_tenant(conn, context.tenant_id)
    sql = f"{_GROUP_SQL} AND g.status = %s AND v.status = ANY(%s)"
    params: list[Any] = [context.tenant_id, status, list(OPERABLE_STATUSES)]
    if code is not None:
        sql += " AND g.code = %s"
        params.append(code)
    # 排序补唯一键：只按 code 不保证稳定顺序（裁决 6）
    sql += " ORDER BY g.code ASC, g.group_id ASC"
    rows = conn.execute(sql, params).fetchall()
    policies = _policies_by_group(conn, context.tenant_id, [UUID(str(row[0])) for row in rows])

    # 过滤先于分页：ACL 在应用层判定，所以先全量过滤再切片
    visible = [
        row
        for row in rows
        if _visible(context, str(row[9]), policies.get(str(row[0]), []))
    ]
    total = len(visible)
    page = visible[offset : offset + limit]
    candidates = _candidates(conn, context.tenant_id, [UUID(str(row[0])) for row in page])
    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "items": [_group_view(row, candidates.get(str(row[0]), [])) for row in page],
    }


@router.post("/v1/catalog/conflicts/{group_id}/resolve", summary="对目录冲突待办做决议")
def resolve_conflict(
    group_id: str, body: ResolveBody, context: PrincipalDep, conn: ConnDep
) -> dict[str, Any]:
    context.require_scope("catalog.read")
    context.require_scope("catalog.resolve")

    try:
        parsed = UUID(group_id)
    except ValueError as exc:
        raise ApiError(ErrorCode.VALIDATION_ERROR, detail="group_id 不是合法 UUID") from exc

    # 输入契约：KEEP 必须带候选；DROP_ALL 必须不带（显式 null 也不行）
    if body.action == "DROP_ALL":
        if "candidate_id" in body.model_fields_set:
            raise ApiError(ErrorCode.VALIDATION_ERROR, detail="DROP_ALL 不得携带 candidate_id")
    elif body.candidate_id is None:
        raise ApiError(ErrorCode.VALIDATION_ERROR, detail="KEEP 必须指定 candidate_id")

    bind_tenant(conn, context.tenant_id)
    row = conn.execute(_GROUP_SQL + " AND g.group_id = %s", (context.tenant_id, parsed)).fetchone()
    policies = _policies_by_group(conn, context.tenant_id, [parsed]) if row is not None else {}
    if row is None or not _visible(context, str(row[9]), policies.get(str(parsed), [])):
        # 不存在、跨租户、scope 不符、ACL 拒绝 → 同一个 404，不给探测面
        raise ApiError(ErrorCode.NOT_FOUND, detail="conflict group not found")

    # 访问已获准：此时版本不可操作才是 409（不能提前隐藏成 404）
    if str(row[8]) not in OPERABLE_STATUSES:
        raise ApiError(ErrorCode.VERSION_CONFLICT, detail=f"version status {row[8]}")

    try:
        require_operator(conn, context.tenant_id, context.principal_id)
    except ConflictError as exc:
        raise ApiError(ErrorCode.ACCESS_DENIED, detail=str(exc)) from exc

    if str(row[3]) != "OPEN":
        raise ApiError(ErrorCode.VERSION_CONFLICT, detail="already resolved")

    try:
        with conn.transaction():
            group = resolve_group(
                conn,
                context.tenant_id,
                version_id=UUID(str(row[1])),
                code=str(row[2]),
                actor_id=context.principal_id,
                candidate_id=body.candidate_id,
                drop_all=body.action == "DROP_ALL",
                note=body.note,
            )
    except ConflictError as exc:
        # 并发/重复：先到者赢；后到者若是版本状态问题 → 409，否则是候选不合法 → 422
        current = get_group(conn, context.tenant_id, parsed)
        if current.status != "OPEN":
            raise ApiError(ErrorCode.VERSION_CONFLICT, detail=str(exc)) from exc
        raise ApiError(ErrorCode.VALIDATION_ERROR, detail=str(exc)) from exc

    return {
        "group_id": str(group.group_id),
        "version_id": str(group.version_id),
        "code": group.code,
        "status": group.status,
        "note": group.note,
        "resolved_by": str(group.resolved_by) if group.resolved_by else None,
        "selected_candidate_id": (
            str(group.selected_candidate_id) if group.selected_candidate_id else None
        ),
        "candidates": [
            {
                "candidate_id": str(candidate.candidate_id),
                "chunk_id": str(candidate.chunk_id),
                "spec": candidate.spec,
                "raw_price": candidate.raw_price,
                "locator": candidate.locator,
            }
            for candidate in group.candidates
        ],
    }
