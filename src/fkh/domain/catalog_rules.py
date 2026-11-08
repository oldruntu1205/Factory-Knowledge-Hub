"""实例（租户）级目录规则：货号正则 + 允许货币。

L1 2026-11-08 裁决要点（实现依据）：
- **缺规则 → 拒绝目录入库**，返回可识别的配置错误；不回落内置工厂默认；
  但**读路径不受影响**（已有目录照常可查、可作答）。
- DB 只保证通用数据形状；业务规则由应用层统一校验，**所有写入路径共用**：
  入库抽取、冲突决议、回填 CLI。
- 正则统一用 Python `re` 的**整串匹配**（`fullmatch`），限制长度与匹配成本。
- PUT 需要审计（谁、何时、前后规则、版本号），且与更新**原子**；相同内容视为无变更。
- 收紧规则使存量数据失效 → 拒绝（先处置数据）。
- 并发：在规则行上 `FOR UPDATE`，PUT 的存量检查与目录写入按租户协调。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import psycopg

MAX_PATTERN_LENGTH = 200
CURRENCY_PATTERN = re.compile(r"^[A-Z]{3}$")
FACTORY_PATTERN = "^[A-Z]{1,3}[0-9]{5,8}$"


class CatalogRuleError(ValueError):
    """规则内容非法（格式/货币集合/正则不可编译）。"""


class CatalogRuleMissing(LookupError):
    """本实例未配置目录规则：目录写入被拒（读取不受影响）。"""


class CatalogRuleConflict(RuntimeError):
    """新规则会使存量数据失效，或并发更新冲突。"""


@dataclass(frozen=True, slots=True)
class CatalogRule:
    code_pattern: str
    currencies: tuple[str, ...]
    matcher: re.Pattern[str]

    def accepts_code(self, code: str) -> bool:
        return self.matcher.fullmatch(code) is not None

    def accepts_currency(self, currency: str) -> bool:
        return currency in self.currencies

    def to_json(self) -> dict[str, Any]:
        return {"code_pattern": self.code_pattern, "currencies": list(self.currencies)}


def compile_rule(code_pattern: str, currencies: list[str] | tuple[str, ...]) -> CatalogRule:
    if not isinstance(code_pattern, str) or not code_pattern.strip():
        raise CatalogRuleError("code_pattern 不能为空")
    pattern = code_pattern.strip()
    if len(pattern) > MAX_PATTERN_LENGTH:
        raise CatalogRuleError(f"code_pattern 过长（上限 {MAX_PATTERN_LENGTH} 字符）")
    try:
        # 统一用 fullmatch 语义：显式锚定，避免调用方各自解释
        matcher = re.compile(pattern)
    except re.error as exc:
        raise CatalogRuleError(f"code_pattern 不是合法正则：{exc}") from exc
    if not currencies:
        raise CatalogRuleError("currencies 不能为空")
    normalized: list[str] = []
    for currency in currencies:
        if not isinstance(currency, str) or not CURRENCY_PATTERN.match(currency):
            raise CatalogRuleError(f"货币码必须是三位大写字母：{currency!r}")
        if currency in normalized:
            raise CatalogRuleError(f"货币码重复：{currency}")
        normalized.append(currency)
    return CatalogRule(code_pattern=pattern, currencies=tuple(sorted(normalized)), matcher=matcher)


def _bind(conn: psycopg.Connection[Any], tenant_id: UUID) -> None:
    """RLS 生效需要会话已绑定租户；本模块自绑定，避免调用方漏绑后误判"未配置"。"""
    conn.execute("SELECT set_config('app.tenant_id', %s, false)", (str(tenant_id),))


def load_rule(
    conn: psycopg.Connection[Any], tenant_id: UUID, *, for_write: bool = False
) -> CatalogRule | None:
    """读规则；未配置返回 None（读路径可继续，写路径需 `require_rule`）。

    `for_write=True` 时对规则行取 **FOR SHARE** 并持有到事务结束：
    与 `save_rule` 的 `FOR UPDATE` 互斥，保证"写入所用的规则不会在提交前被改掉"，
    也保证 PUT 的存量检查能看到先提交的写入。
    """
    _bind(conn, tenant_id)
    sql = "SELECT code_pattern, currencies FROM catalog_rules WHERE tenant_id = %s"
    if for_write:
        sql += " FOR SHARE"
    row = conn.execute(sql, (tenant_id,)).fetchone()
    if row is None:
        return None
    return compile_rule(str(row[0]), list(row[1]))


def require_rule(
    conn: psycopg.Connection[Any], tenant_id: UUID, *, for_write: bool = False
) -> CatalogRule:
    rule = load_rule(conn, tenant_id, for_write=for_write)
    if rule is None:
        raise CatalogRuleMissing(
            "本实例尚未配置目录规则（货号正则与货币集合）；"
            "请先通过 PUT /v1/config/catalog-rules 配置后再导入目录"
        )
    return rule


def save_rule(
    conn: psycopg.Connection[Any],
    tenant_id: UUID,
    *,
    actor_id: UUID,
    code_pattern: str,
    currencies: list[str] | tuple[str, ...],
    note: str | None = None,
) -> tuple[CatalogRule, bool]:
    """保存规则；返回 (规则, 是否发生变化)。

    同一事务内：锁规则行 → 校验 → 存量兼容性检查 → 更新 → 写审计。
    相同内容直接返回 unchanged，不产生审计版本。
    """
    new_rule = compile_rule(code_pattern, currencies)
    _require_read_committed(conn)
    _bind(conn, tenant_id)

    # 锁规则行（不存在时锁租户行，避免并发首次创建产生两条）
    locked = conn.execute(
        "SELECT code_pattern, currencies FROM catalog_rules WHERE tenant_id = %s FOR UPDATE",
        (tenant_id,),
    ).fetchone()
    if locked is None:
        conn.execute(
            "SELECT 1 FROM tenants WHERE id = %s FOR UPDATE", (tenant_id,)
        ).fetchone()

    current = None
    if locked is not None:
        current = compile_rule(str(locked[0]), list(locked[1]))
        if (
            current.code_pattern == new_rule.code_pattern
            and current.currencies == new_rule.currencies
        ):
            return current, False

    _assert_compatible(conn, tenant_id, new_rule)

    conn.execute(
        """INSERT INTO catalog_rules (tenant_id, code_pattern, currencies, updated_at)
           VALUES (%s, %s, %s, now())
           ON CONFLICT (tenant_id) DO UPDATE
             SET code_pattern = EXCLUDED.code_pattern,
                 currencies = EXCLUDED.currencies,
                 updated_at = now()""",
        (tenant_id, new_rule.code_pattern, list(new_rule.currencies)),
    )

    revision_no = conn.execute(
        """SELECT coalesce(max(revision_no), 0) + 1 FROM catalog_rule_revisions
            WHERE tenant_id = %s""",
        (tenant_id,),
    ).fetchone()
    assert revision_no is not None
    conn.execute(
        """INSERT INTO catalog_rule_revisions
             (tenant_id, revision_no, before_rule, after_rule, actor_id)
           VALUES (%s, %s, %s, %s, %s)""",
        (
            tenant_id,
            revision_no[0],
            json.dumps(current.to_json()) if current is not None else None,
            json.dumps({**new_rule.to_json(), "note": note}),
            actor_id,
        ),
    )
    return new_rule, True


def _require_read_committed(conn: psycopg.Connection[Any]) -> None:
    """PUT 的存量检查依赖 READ COMMITTED（每条语句取新快照）。

    在 REPEATABLE READ 下，等待排他锁后再检查仍看到旧快照，会放过刚提交的不兼容数据；
    此时宁可拒绝写入，也不假装"锁住了就一定安全"。
    """
    row = conn.execute("SHOW transaction_isolation").fetchone()
    level = str(row[0]) if row is not None else ""
    if level.strip().lower() != "read committed":
        raise CatalogRuleConflict(
            "目录规则更新要求 READ COMMITTED 隔离级别（当前 "
            f"{level}）：该级别下等待锁后能看到新提交的数据"
        )


def _assert_compatible(
    conn: psycopg.Connection[Any], tenant_id: UUID, rule: CatalogRule
) -> None:
    """收紧规则前检查存量数据；不兼容则拒绝（先处置数据）。"""
    codes = conn.execute(
        """SELECT DISTINCT code FROM catalog_items WHERE tenant_id = %s
           UNION
           SELECT DISTINCT code FROM catalog_conflict_groups WHERE tenant_id = %s""",
        (tenant_id, tenant_id),
    ).fetchall()
    bad = [str(row[0]) for row in codes if not rule.accepts_code(str(row[0]))]
    if bad:
        sample = ", ".join(sorted(bad)[:5])
        raise CatalogRuleConflict(
            f"{len(bad)} 个存量货号不符合新规则（示例：{sample}）；请先处置这些数据再收紧规则"
        )
    bad_currency = conn.execute(
        "SELECT DISTINCT currency FROM catalog_items WHERE tenant_id = %s"
        " AND NOT (currency = ANY(%s))",
        (tenant_id, list(rule.currencies)),
    ).fetchall()
    if bad_currency:
        sample = ", ".join(sorted(str(row[0]) for row in bad_currency))
        raise CatalogRuleConflict(
            f"存量目录使用了不在新货币集合里的货币（{sample}）；请先处置这些数据"
        )


__all__ = [
    "CatalogRule",
    "CatalogRuleConflict",
    "CatalogRuleError",
    "CatalogRuleMissing",
    "FACTORY_PATTERN",
    "compile_rule",
    "load_rule",
    "require_rule",
    "save_rule",
]
