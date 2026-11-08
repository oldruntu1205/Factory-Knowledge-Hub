"""实例级配置端点：知识域清单（ADR-009）。

- `GET  /v1/config/domains`（`knowledge.read`）：列出本实例登记的域
- `POST /v1/config/domains`（`knowledge.write`）：登记新域

租户一律取自认证上下文，**不接受请求体里的 tenant**。
权限含义（L1 2026-11-08 确认）：拥有知识写权限的人，可以向本实例的共享分类清单追加域；
本轮只有登记能力，没有改名/停用/删除。
"""

from __future__ import annotations

from typing import Annotated, Any

import psycopg
from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from fkh.api.db import get_conn
from fkh.api.errors import ApiError, ErrorCode
from fkh.api.security import PrincipalDep
from fkh.domain.domains import (
    DomainConflict,
    DomainError,
    DomainService,
    RegisterOutcome,
)

router = APIRouter(tags=["config"])

ConnDep = Annotated[psycopg.Connection[Any], Depends(get_conn)]


class DomainRegisterRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=2, max_length=64)
    label: str = Field(min_length=1, max_length=120)


@router.get("/v1/config/domains", summary="列出本实例的知识域清单")
def list_domains(context: PrincipalDep, conn: ConnDep) -> dict[str, Any]:
    context.require_scope("knowledge.read")
    service = DomainService(conn, context.tenant_id)
    return {
        "domains": [
            {
                "code": item.code,
                "label": item.label,
                "is_active": item.is_active,
                "is_default": item.is_default,
            }
            for item in service.list_domains()
        ]
    }


@router.post("/v1/config/domains", status_code=201, summary="登记新的知识域（幂等）")
def register_domain(
    payload: DomainRegisterRequest, context: PrincipalDep, conn: ConnDep
) -> dict[str, Any]:
    context.require_scope("knowledge.write")
    service = DomainService(conn, context.tenant_id)
    try:
        outcome = service.register(code=payload.code, label=payload.label)
    except DomainConflict as exc:
        raise ApiError(ErrorCode.VERSION_CONFLICT, detail=str(exc)) from exc
    except DomainError as exc:
        raise ApiError(
            ErrorCode.VALIDATION_ERROR,
            detail=str(exc),
            hint="domain.code: lowercase letters/digits/underscore, starting with a letter",
        ) from exc
    return {
        "code": payload.code,
        "label": payload.label,
        "outcome": str(outcome),
        "created": outcome is RegisterOutcome.CREATED,
    }


__all__ = ["router"]
