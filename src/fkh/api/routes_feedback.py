"""反馈与坏例登记（`POST /v1/feedback`，L1 §6.3）。

边界：
- 只登记**已经发生**的一次问答（按 trace_id 找 `rag_queries`），不接收原始 query；
- `redacted_case` 只存 trace_id / query_hash / category，不落明文；
- `expected_behavior` 是封闭结构（枚举 + 布尔），不接受自由文本；
- 权限 `feedback.create` 是**租户级登记权限**：可关联本租户任意主体的问答，
  不限制为本人；`query_hash` 不是匿名化，不能拿它扩大授权；
- 租户来自认证上下文；跨租户 trace 与不存在同样 404；
- 同一 trace 允许重复登记（无唯一约束），便于多人从不同角度反馈；
- `rag.bad_case.created.v1` outbox 事件尚未接线，坏例回归消费端另立切片。
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

import psycopg
from fastapi import APIRouter, Depends
from psycopg.types.json import Jsonb

from fkh.api.db import get_conn
from fkh.api.errors import ApiError, ErrorCode
from fkh.api.models import FeedbackRequest
from fkh.api.security import PrincipalDep

router = APIRouter(tags=["knowledge"])

ConnDep = Annotated[psycopg.Connection[Any], Depends(get_conn)]


@router.post("/v1/feedback", status_code=201, summary="反馈与坏例登记")
def create_feedback(
    payload: FeedbackRequest,
    context: PrincipalDep,
    conn: ConnDep,
) -> dict[str, Any]:
    context.require_scope("feedback.create")

    # 只允许登记本租户真实发生过的 trace；找不到/跨租户一律 404，不泄露存在性。
    row = conn.execute(
        """SELECT id, query_hash
             FROM rag_queries
            WHERE tenant_id = %s AND trace_id = %s
            ORDER BY created_at DESC, id DESC
            LIMIT 1""",
        (context.tenant_id, payload.trace_id),
    ).fetchone()
    if row is None:
        raise ApiError(ErrorCode.NOT_FOUND, detail=f"trace_id 不存在: {payload.trace_id}")

    bad_case_id = uuid.uuid4()
    redacted_case = {
        "trace_id": payload.trace_id,
        "query_hash": str(row[1]),
        "category": payload.category,
    }
    conn.execute(
        """INSERT INTO bad_cases
             (tenant_id, id, query_id, category, severity, redacted_case,
              expected_behavior, regression_status)
           VALUES (%s, %s, %s, %s, 'P2', %s, %s, 'OPEN')""",
        (
            context.tenant_id,
            bad_case_id,
            row[0],
            payload.category,
            Jsonb(redacted_case),
            Jsonb(payload.expected_behavior.model_dump(mode="json")),
        ),
    )
    return {
        "bad_case_id": str(bad_case_id),
        "trace_id": payload.trace_id,
        "category": payload.category,
        "severity": "P2",
        "regression_status": "OPEN",
    }


__all__ = ["router"]
