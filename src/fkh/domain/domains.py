"""实例级知识域清单（ADR-009：一客户一实例，域由实例配置）。

设计口径（L1 2026-11-08 有条件通过）：
- 文档对域有**复合外键**，库层守住"只能使用本租户已登记的域"；
- 域只做组织 / 展示 / 过滤，**不参与授权与可见性**（那仍由 status/audience/scope/ACL 决定）；
- 新租户域清单可以为空，导入前须由实例初始化登记（工厂模板只在迁移里给已有租户种子）。

登记语义（幂等）：
- 首次创建 → CREATED（201）
- 相同 code + 相同 label → EXISTS（200）
- 相同 code + 不同 label → CONFLICT（409，不静默改名）
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from uuid import UUID

import psycopg

CODE_PATTERN = re.compile(r"^[a-z][a-z0-9_]{1,63}$")


class DomainError(ValueError):
    """域登记被拒（格式/内容非法）。"""


class DomainConflict(RuntimeError):
    """同 code 已存在但 label 不同（不静默改名）。"""


class DomainMissing(LookupError):
    """该实例没有登记这个域（导入时应 422 并提示登记）。"""


class RegisterOutcome(StrEnum):
    CREATED = "CREATED"
    EXISTS = "EXISTS"


@dataclass(frozen=True, slots=True)
class DomainView:
    code: str
    label: str
    is_active: bool
    is_default: bool


class DomainService:
    def __init__(self, conn: psycopg.Connection[Any], tenant_id: UUID) -> None:
        self._conn = conn
        self._tenant = tenant_id
        self._bind()

    def _bind(self) -> None:
        self._conn.execute("SELECT set_config('app.tenant_id', %s, true)", (str(self._tenant),))

    def list_domains(self) -> list[DomainView]:
        rows = self._conn.execute(
            """SELECT code, label, is_active, is_default FROM knowledge_domains
                WHERE tenant_id = %s ORDER BY is_default DESC, code""",
            (self._tenant,),
        ).fetchall()
        return [
            DomainView(code=str(r[0]), label=str(r[1]), is_active=bool(r[2]), is_default=bool(r[3]))
            for r in rows
        ]

    def register(self, *, code: str, label: str) -> RegisterOutcome:
        if not CODE_PATTERN.match(code):
            raise DomainError("domain code 必须是小写字母开头、只含小写字母/数字/下划线，长度 2–64")
        if not label.strip():
            raise DomainError("domain label 不能为空")
        existing = self._conn.execute(
            "SELECT label FROM knowledge_domains WHERE tenant_id = %s AND code = %s",
            (self._tenant, code),
        ).fetchone()
        if existing is not None:
            if str(existing[0]) != label:
                raise DomainConflict(
                    f"domain {code} 已存在（label={existing[0]}），不支持静默改名"
                )
            return RegisterOutcome.EXISTS
        self._conn.execute(
            """INSERT INTO knowledge_domains (tenant_id, code, label, is_default)
               VALUES (%s, %s, %s, false)
               ON CONFLICT (tenant_id, code) DO NOTHING""",
            (self._tenant, code, label),
        )
        # 并发下另一事务可能先插入：以库内结果为准，避免"看起来创建成功但实际是别人的"
        row = self._conn.execute(
            "SELECT label FROM knowledge_domains WHERE tenant_id = %s AND code = %s",
            (self._tenant, code),
        ).fetchone()
        if row is None or str(row[0]) != label:
            raise DomainConflict(f"domain {code} 并发登记冲突")
        return RegisterOutcome.CREATED

    def assert_usable(self, code: str) -> None:
        """导入/更新时校验：必须已登记且启用；域不授予任何访问权限。"""
        if not CODE_PATTERN.match(code):
            raise DomainError("domain 格式非法：需小写字母开头，只含小写字母/数字/下划线")
        row = self._conn.execute(
            "SELECT is_active FROM knowledge_domains WHERE tenant_id = %s AND code = %s",
            (self._tenant, code),
        ).fetchone()
        if row is None:
            raise DomainMissing(
                f"domain {code} 未在该实例登记（可通过 POST /v1/config/domains 添加）"
            )
        if not bool(row[0]):
            raise DomainMissing(f"domain {code} 已停用，不能用于新文档")


__all__ = [
    "CODE_PATTERN",
    "DomainConflict",
    "DomainError",
    "DomainMissing",
    "DomainService",
    "DomainView",
    "RegisterOutcome",
]
