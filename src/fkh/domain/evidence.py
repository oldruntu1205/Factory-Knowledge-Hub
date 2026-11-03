"""受控证据定位：把一条 evidence_id 解析为可读的证据，并落访问审计。

判定顺序（任一不满足即「不可用」，对外一律 403 且 body 相同）：
1. 租户一致——不依赖 RLS 兜底，跨租户在应用层先拒；
2. 版本状态与 audience 的组合必须合法：
   `APPROVED_INTERNAL + internal` 或 `PUBLISHED_PUBLIC + customer_public`，其余拒绝；
3. `required_scope` 已赋值且被令牌 `allowed_scopes` 包含——未赋值即拒绝，无默认；
4. 片段 ACL 允许——Deny 优先、无匹配拒绝、策略缺失拒绝；
5. 若返回 parent 全文（含**直接按 parent id 请求**），parent 自身与其下**全部** child
   必须通过同一组租户/版本/scope/ACL 检查；拿不到完整 child 清单就只返回 child。

签名链接：ADR-01 未签、无对象存储 ⇒ `signed_url=None` + `signed_url_state='NOT_CONFIGURED'`。
这是对原契约「返回短期签名链接」的显式修订，已在 docs/SPEC/evidence-locator.md 登记。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from uuid import UUID

import psycopg

from fkh.authz.parent import ChunkAcl, evaluate_parent_restore
from fkh.authz.policy import AclPolicy, Decision, Subject, evaluate
from fkh.domain.state_machine import VersionStatus

SIGNED_URL_NOT_CONFIGURED = "NOT_CONFIGURED"

# 状态与 audience 的合法组合。登记外的组合一律拒绝——包括 SUPERSEDED。
VISIBLE_COMBINATIONS: dict[VersionStatus, str] = {
    VersionStatus.APPROVED_INTERNAL: "internal",
    VersionStatus.PUBLISHED_PUBLIC: "customer_public",
}

# audience → 允许的 required_scope 集合。仅用于校验列本身合法；
# 真正决定能否读取的是令牌 allowed_scopes 是否包含该值。
ALLOWED_REQUIRED_SCOPES: dict[str, frozenset[str]] = {
    "internal": frozenset({"internal_product", "internal_training"}),
    "customer_public": frozenset({"customer_public"}),
}

# locator 只放行已知安全的定位维度。白名单而不是黑名单：
# 未来新增字段若可能含存储路径，默认被丢掉，而不是默认泄露。
SAFE_LOCATOR_KEYS: tuple[str, ...] = (
    "block_index",
    "page_start",
    "page_end",
    "paragraph_index",
    "table_index",
    "row_index",
    "bbox",
)


class DenyReason(StrEnum):
    """审计用拒绝原因。对外不暴露，只用于内部追责与统计。"""

    RESOURCE_UNAVAILABLE = "RESOURCE_UNAVAILABLE"
    STATE_NOT_VISIBLE = "STATE_NOT_VISIBLE"
    SCOPE_NOT_ALLOWED = "SCOPE_NOT_ALLOWED"
    ACL_DENIED = "ACL_DENIED"
    PARENT_INCOMPLETE = "PARENT_INCOMPLETE"


class EvidenceNotAvailable(LookupError):
    """不存在、跨租户、状态不可见、scope 不符或 ACL 拒绝。对外必须不可区分。"""

    def __init__(self, reason: DenyReason) -> None:
        super().__init__(reason.value)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class EvidenceView:
    evidence_id: UUID
    document_id: UUID
    version_id: UUID
    status: str
    classification: int
    audience: str
    locator: dict[str, Any] | None
    heading_path: tuple[str, ...] | None
    text: str
    text_level: str
    text_evidence_id: UUID
    signed_url: str | None
    signed_url_state: str


@dataclass(frozen=True, slots=True)
class _Chunk:
    id: UUID
    version_id: UUID
    parent_chunk_id: UUID | None
    chunk_kind: str
    chunk_text: str
    locator: dict[str, Any] | None
    heading_path: tuple[str, ...] | None
    policy_json: Any | None


@dataclass(frozen=True, slots=True)
class _Version:
    document_id: UUID
    status: VersionStatus
    classification: int
    audience: str
    required_scope: str | None


_CHUNK_SQL = """
SELECT c.id, c.version_id, c.parent_chunk_id, c.chunk_kind, c.chunk_text, c.locator,
       c.heading_path, p.policy_json,
       v.document_id, v.status, v.classification, v.audience, v.required_scope
  FROM knowledge_chunks c
  JOIN knowledge_versions v
    ON v.tenant_id = c.tenant_id AND v.id = c.version_id
  LEFT JOIN acl_policies p
    ON p.tenant_id = c.tenant_id AND p.id = c.acl_policy_id
 WHERE c.tenant_id = %s AND c.id = %s
"""

_PARENT_SQL = """
SELECT c.id, c.version_id, c.parent_chunk_id, c.chunk_kind, c.chunk_text, c.locator,
       c.heading_path, p.policy_json
  FROM knowledge_chunks c
  LEFT JOIN acl_policies p
    ON p.tenant_id = c.tenant_id AND p.id = c.acl_policy_id
 WHERE c.tenant_id = %s AND (c.id = %s OR c.parent_chunk_id = %s)
"""

_AUDIT_SQL = """
INSERT INTO evidence_access_log
  (tenant_id, principal_id, evidence_id, text_evidence_id, result, reason, trace_id)
VALUES (%s, %s, %s, %s, %s, %s, %s)
"""


class EvidenceService:
    def __init__(self, conn: psycopg.Connection[Any], tenant_id: UUID) -> None:
        self._conn = conn
        self._tenant = tenant_id
        self._bind_tenant()

    def _bind_tenant(self) -> None:
        self._conn.execute("SELECT set_config('app.tenant_id', %s, true)", (str(self._tenant),))

    def resolve(
        self,
        evidence_id: UUID,
        *,
        subject: Subject,
        allowed_scopes: frozenset[str],
        trace_id: str,
    ) -> EvidenceView:
        """解析证据；允许时先落审计再返回，拒绝时落审计后抛 `EvidenceNotAvailable`。"""
        self._bind_tenant()
        principal_id = UUID(subject.principal_id)
        try:
            view = self._resolve(evidence_id, subject=subject, allowed_scopes=allowed_scopes)
        except EvidenceNotAvailable as denied:
            self._audit(
                principal_id,
                evidence_id,
                None,
                "DENIED",
                denied.reason.value,
                trace_id,
            )
            # 拒绝也要可靠留痕：异常路径会回滚请求事务，所以这里必须显式提交
            self._conn.commit()
            raise

        self._audit(
            principal_id,
            evidence_id,
            view.text_evidence_id,
            "ALLOWED",
            "AUTHORIZED",
            trace_id,
        )
        # 审计必须先于正文离开进程：请求级连接在响应发送后才回收，不能依赖它提交
        self._conn.commit()
        return view

    # ---- 内部 --------------------------------------------------------------
    def _resolve(
        self,
        evidence_id: UUID,
        *,
        subject: Subject,
        allowed_scopes: frozenset[str],
    ) -> EvidenceView:
        row = self._conn.execute(_CHUNK_SQL, (self._tenant, evidence_id)).fetchone()
        if row is None:
            # 不额外查其他租户来区分「不存在」与「跨租户」：那本身就是一次越权探测
            raise EvidenceNotAvailable(DenyReason.RESOURCE_UNAVAILABLE)

        chunk = _chunk(row[:8])
        version = _Version(
            document_id=UUID(str(row[8])),
            status=VersionStatus(row[9]),
            classification=int(row[10]),
            audience=str(row[11]),
            required_scope=str(row[12]) if row[12] is not None else None,
        )

        _assert_version_readable(version, allowed_scopes=allowed_scopes)

        if not _allowed(_policy(chunk.policy_json), subject, self._tenant):
            raise EvidenceNotAvailable(DenyReason.ACL_DENIED)

        text_source = chunk
        text_level = "CHILD"

        if chunk.chunk_kind == "parent":
            # 直接按 parent id 请求：必须与「从 child 恢复」走同一套子块检查
            if not self._all_children_readable(chunk, subject):
                raise EvidenceNotAvailable(DenyReason.PARENT_INCOMPLETE)
            text_level = "PARENT"
        elif chunk.parent_chunk_id is not None:
            parent = self._load_parent(chunk.parent_chunk_id, subject)
            if parent is not None:
                text_source = parent
                text_level = "PARENT"

        return EvidenceView(
            evidence_id=chunk.id,
            document_id=version.document_id,
            version_id=chunk.version_id,
            status=version.status.value,
            classification=version.classification,
            audience=version.audience,
            locator=_safe_locator(text_source.locator),
            heading_path=text_source.heading_path,
            text=text_source.chunk_text,
            text_level=text_level,
            text_evidence_id=text_source.id,
            signed_url=None,
            signed_url_state=SIGNED_URL_NOT_CONFIGURED,
        )

    def _load_parent(self, parent_id: UUID, subject: Subject) -> _Chunk | None:
        """返回可整体恢复的父块；无法证明「全部 child 可见」时返回 None。"""
        parent, children = self._load_family(parent_id)
        if parent is None or parent.chunk_kind != "parent" or not children:
            return None
        return parent if self._family_readable(parent, children, subject) else None

    def _all_children_readable(self, parent: _Chunk, subject: Subject) -> bool:
        loaded, children = self._load_family(parent.id)
        if loaded is None or not children:
            return False
        return self._family_readable(loaded, children, subject)

    def _family_readable(self, parent: _Chunk, children: list[_Chunk], subject: Subject) -> bool:
        parent_acl = ChunkAcl(
            chunk_id=str(parent.id),
            tenant_id=str(self._tenant),
            parent_chunk_id=None,
            policy=_policy(parent.policy_json),
        )
        siblings: list[ChunkAcl] = []
        for child in children:
            if child.version_id != parent.version_id:
                # 父子跨版本说明聚合数据有问题，按不可恢复处理而不是照常返回
                return False
            siblings.append(
                ChunkAcl(
                    chunk_id=str(child.id),
                    tenant_id=str(self._tenant),
                    parent_chunk_id=str(parent.id),
                    policy=_policy(child.policy_json),
                )
            )
        return evaluate_parent_restore(parent_acl, siblings, subject).allowed

    def _load_family(self, parent_id: UUID) -> tuple[_Chunk | None, list[_Chunk]]:
        rows = self._conn.execute(_PARENT_SQL, (self._tenant, parent_id, parent_id)).fetchall()
        parent: _Chunk | None = None
        children: list[_Chunk] = []
        for row in rows:
            chunk = _chunk(row)
            if chunk.id == parent_id:
                parent = chunk
            else:
                children.append(chunk)
        return parent, children

    def _audit(
        self,
        principal_id: UUID,
        evidence_id: UUID,
        text_evidence_id: UUID | None,
        result: str,
        reason: str,
        trace_id: str,
    ) -> None:
        self._conn.execute(
            _AUDIT_SQL,
            (
                self._tenant,
                principal_id,
                evidence_id,
                text_evidence_id,
                result,
                reason,
                trace_id,
            ),
        )


def _assert_version_readable(version: _Version, *, allowed_scopes: frozenset[str]) -> None:
    expected_audience = VISIBLE_COMBINATIONS.get(version.status)
    if expected_audience is None or version.audience != expected_audience:
        raise EvidenceNotAvailable(DenyReason.STATE_NOT_VISIBLE)

    if version.required_scope is None:
        # 未由业务确认赋值：不设默认值，直接拒绝
        raise EvidenceNotAvailable(DenyReason.SCOPE_NOT_ALLOWED)
    if version.required_scope not in ALLOWED_REQUIRED_SCOPES[version.audience]:
        raise EvidenceNotAvailable(DenyReason.SCOPE_NOT_ALLOWED)
    if version.required_scope not in allowed_scopes:
        raise EvidenceNotAvailable(DenyReason.SCOPE_NOT_ALLOWED)


def _allowed(policy: AclPolicy | None, subject: Subject, tenant_id: UUID) -> bool:
    decision, _reason = evaluate(policy, subject, resource_tenant_id=str(tenant_id))
    return decision is Decision.ALLOW


def _chunk(row: tuple[Any, ...]) -> _Chunk:
    return _Chunk(
        id=UUID(str(row[0])),
        version_id=UUID(str(row[1])),
        parent_chunk_id=UUID(str(row[2])) if row[2] is not None else None,
        chunk_kind=str(row[3]),
        chunk_text=str(row[4]),
        locator=dict(row[5]) if row[5] is not None else None,
        heading_path=tuple(str(part) for part in row[6]) if row[6] is not None else None,
        policy_json=row[7],
    )


def _safe_locator(locator: dict[str, Any] | None) -> dict[str, Any] | None:
    """只保留白名单字段，防止 locator 内嵌存储路径或哈希。"""
    if locator is None:
        return None
    return {key: locator[key] for key in SAFE_LOCATOR_KEYS if key in locator}


def _policy(raw: Any) -> AclPolicy | None:
    """策略缺失返回 None（由 evaluate 判为拒绝）；格式非法直接抛错，不静默降级。"""
    if raw is None:
        return None
    return AclPolicy.from_json(raw)


__all__ = [
    "ALLOWED_REQUIRED_SCOPES",
    "DenyReason",
    "EvidenceNotAvailable",
    "EvidenceService",
    "EvidenceView",
    "SAFE_LOCATOR_KEYS",
    "SIGNED_URL_NOT_CONFIGURED",
    "VISIBLE_COMBINATIONS",
]
