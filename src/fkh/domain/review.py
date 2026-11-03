"""审核、内部批准、对外发布与撤销。

职责边界：
- 只做状态推进与审计，不解析内容、不生成索引；
- 对外发布要求两个不同主体（技术审批 + 公开审批），由本模块强制，数据库 CHECK 兜底；
- 撤销立即生效：状态一改，后续读取就查不到该版本，不等索引清理完成。

行锁：所有写操作先用 `SELECT ... FOR UPDATE` 取当前状态。没有这把锁，两个审核人
同时点"通过"会依次读到同一个旧状态，各自执行一次迁移，审计里出现两条互相矛盾的记录。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

from fkh.domain.state_machine import (
    IllegalTransition,
    VersionStatus,
    assert_publishable,
    assert_transition,
)


class VersionNotFound(LookupError):
    """版本不存在，或不属于当前租户。两种情况对外不可区分，避免用于探测。"""


class ReviewConflict(ValueError):
    """当前状态不允许该操作。"""


# 同一聚合版本内的事件顺序：按**业务阶段**排，不按写入时刻排。
#
# 发布与撤销同属一个版本（aggregate_version = version_no），outbox 的顺序唯一键是
# (tenant, aggregate_type, aggregate_id, aggregate_version, event_ordinal)。
# 两者都用默认 0 会被唯一约束拒绝；改用 occurred_at 或 UUID 排序又会与业务顺序相反
# （同一事务内 now() 恒定、UUID 随机）。所以顺序必须由生产者在写入时显式给出。
VERSION_EVENT_ORDINAL: dict[str, int] = {
    "knowledge.version.approved.v1": 0,
    "knowledge.version.published.v1": 1,
    "knowledge.version.revoked.v1": 2,
}


@dataclass(frozen=True, slots=True)
class VersionSnapshot:
    id: UUID
    status: VersionStatus
    version_no: int
    classification: int
    audience: str
    approved_by: UUID | None
    public_approved_by: UUID | None
    content_sha256: str


class ReviewService:
    def __init__(self, conn: psycopg.Connection[Any], tenant_id: UUID) -> None:
        self._conn = conn
        self._tenant = tenant_id
        self._bind_tenant()

    def _bind_tenant(self) -> None:
        """绑定事务级租户上下文。

        每个公开方法都要重新绑定：`set_config(..., is_local=true)` 只在当前事务内有效，
        调用方一旦 commit，上下文就消失，后续查询会静默返回零行而不是报错。
        把这件事放在构造函数里做，会逼调用方记住"commit 之后要重建服务"。
        """
        self._conn.execute("SELECT set_config('app.tenant_id', %s, true)", (str(self._tenant),))

    # ---- 读取 --------------------------------------------------------------
    def lock(self, version_id: UUID) -> VersionSnapshot:
        self._bind_tenant()
        row = self._conn.execute(
            """SELECT id, status, version_no, classification, audience,
                      approved_by, public_approved_by, content_sha256
                 FROM knowledge_versions
                WHERE tenant_id = %s AND id = %s
                  FOR UPDATE""",
            (self._tenant, version_id),
        ).fetchone()
        if row is None:
            raise VersionNotFound(str(version_id))
        return VersionSnapshot(
            id=UUID(str(row[0])),
            status=VersionStatus(row[1]),
            version_no=int(row[2]),
            classification=int(row[3]),
            audience=str(row[4]),
            approved_by=UUID(str(row[5])) if row[5] else None,
            public_approved_by=UUID(str(row[6])) if row[6] else None,
            content_sha256=str(row[7]),
        )

    def snapshot(self, version_id: UUID) -> VersionSnapshot:
        """不加锁的读取，用于查询与测试断言。"""
        self._bind_tenant()
        row = self._conn.execute(
            """SELECT id, status, version_no, classification, audience,
                      approved_by, public_approved_by, content_sha256
                 FROM knowledge_versions
                WHERE tenant_id = %s AND id = %s""",
            (self._tenant, version_id),
        ).fetchone()
        if row is None:
            raise VersionNotFound(str(version_id))
        return VersionSnapshot(
            id=UUID(str(row[0])),
            status=VersionStatus(row[1]),
            version_no=int(row[2]),
            classification=int(row[3]),
            audience=str(row[4]),
            approved_by=UUID(str(row[5])) if row[5] else None,
            public_approved_by=UUID(str(row[6])) if row[6] else None,
            content_sha256=str(row[7]),
        )

    # ---- 审核 --------------------------------------------------------------
    def pass_review(self, version_id: UUID, actor_id: UUID, reason: str | None = None) -> VersionSnapshot:
        self._bind_tenant()
        version = self.lock(version_id)
        if version.status is not VersionStatus.DRAFT:
            raise ReviewConflict(f"只有 DRAFT 可提交审核，当前为 {version.status}")
        self._transition(version, VersionStatus.REVIEWED, actor_id, reason)
        return self.snapshot(version_id)

    def reject(self, version_id: UUID, actor_id: UUID, reason: str | None = None) -> VersionSnapshot:
        self._bind_tenant()
        version = self.lock(version_id)
        if version.status not in {VersionStatus.REVIEWED, VersionStatus.APPROVED_INTERNAL}:
            raise ReviewConflict(f"当前状态 {version.status} 不可退回草稿")
        # 退回必须清空审批人，否则数据库会拒绝"DRAFT 带审批人"的组合
        action = assert_transition(version.status, VersionStatus.DRAFT)
        self._conn.execute(
            """UPDATE knowledge_versions
                  SET status = %s, approved_by = NULL, public_approved_by = NULL
                WHERE tenant_id = %s AND id = %s""",
            (VersionStatus.DRAFT.value, self._tenant, version_id),
        )
        self._record(version_id, actor_id, action.value, reason)
        return self.snapshot(version_id)

    def approve_internal(
        self, version_id: UUID, actor_id: UUID, reason: str | None = None
    ) -> VersionSnapshot:
        self._bind_tenant()
        version = self.lock(version_id)
        if version.status is not VersionStatus.REVIEWED:
            raise ReviewConflict(f"只有 REVIEWED 可批准为内部可用，当前为 {version.status}")
        action = assert_transition(version.status, VersionStatus.APPROVED_INTERNAL)
        self._conn.execute(
            """UPDATE knowledge_versions
                  SET status = %s, approved_by = %s
                WHERE tenant_id = %s AND id = %s""",
            (VersionStatus.APPROVED_INTERNAL.value, actor_id, self._tenant, version_id),
        )
        self._record(version_id, actor_id, action.value, reason)
        return self.snapshot(version_id)

    def publish_public(
        self,
        version_id: UUID,
        public_approver: UUID,
        reason: str | None = None,
    ) -> VersionSnapshot:
        self._bind_tenant()
        version = self.lock(version_id)

        if version.status is not VersionStatus.APPROVED_INTERNAL:
            raise ReviewConflict(f"只有 APPROVED_INTERNAL 可对外发布，当前为 {version.status}")

        assert_publishable(
            version.status, classification=version.classification, audience=version.audience
        )

        if version.approved_by is None:
            raise ReviewConflict("缺少技术审批人，不能对外发布")
        if version.approved_by == public_approver:
            raise ReviewConflict("公开审批人不得与技术审批人为同一主体")

        action = assert_transition(VersionStatus.APPROVED_INTERNAL, VersionStatus.PUBLISHED_PUBLIC)
        self._conn.execute(
            """UPDATE knowledge_versions
                  SET status = %s, public_approved_by = %s
                WHERE tenant_id = %s AND id = %s""",
            (VersionStatus.PUBLISHED_PUBLIC.value, public_approver, self._tenant, version_id),
        )
        self._record(version_id, public_approver, action.value, reason)

        generation = self._current_generation(version_id)
        self._emit(
            event_type="knowledge.version.published.v1",
            aggregate_id=version_id,
            aggregate_version=version.version_no,
            payload={
                "version_id": str(version_id),
                "audience": "customer_public",
                "index_generation": generation,
                "content_sha256": version.content_sha256,
            },
        )
        return self.snapshot(version_id)

    def revoke(self, version_id: UUID, actor_id: UUID, reason: str | None = None) -> VersionSnapshot:
        self._bind_tenant()
        version = self.lock(version_id)
        if version.status is VersionStatus.REVOKED:
            raise ReviewConflict("版本已撤销")

        action = assert_transition(version.status, VersionStatus.REVOKED)
        self._conn.execute(
            "UPDATE knowledge_versions SET status = %s WHERE tenant_id = %s AND id = %s",
            (VersionStatus.REVOKED.value, self._tenant, version_id),
        )
        self._record(version_id, actor_id, action.value, reason)
        self._emit(
            event_type="knowledge.version.revoked.v1",
            aggregate_id=version_id,
            aggregate_version=version.version_no,
            payload={"version_id": str(version_id), "audience": version.audience},
        )
        return self.snapshot(version_id)

    # ---- 内部 --------------------------------------------------------------
    def _transition(
        self,
        version: VersionSnapshot,
        target: VersionStatus,
        actor_id: UUID,
        reason: str | None,
    ) -> None:
        action = assert_transition(version.status, target)
        self._conn.execute(
            "UPDATE knowledge_versions SET status = %s WHERE tenant_id = %s AND id = %s",
            (target.value, self._tenant, version.id),
        )
        self._record(version.id, actor_id, action.value, reason)

    def _record(self, version_id: UUID, actor_id: UUID, action: str, reason: str | None) -> None:
        self._conn.execute(
            """INSERT INTO review_actions (tenant_id, version_id, actor_id, action, reason)
               VALUES (%s, %s, %s, %s, %s)""",
            (self._tenant, version_id, actor_id, action, reason),
        )

    def _current_generation(self, version_id: UUID) -> int:
        row = self._conn.execute(
            """SELECT coalesce(max(index_generation), 0)
                 FROM knowledge_chunks
                WHERE tenant_id = %s AND version_id = %s""",
            (self._tenant, version_id),
        ).fetchone()
        return int(row[0]) if row is not None else 0

    def _emit(
        self,
        *,
        event_type: str,
        aggregate_id: UUID,
        aggregate_version: int,
        payload: dict[str, Any],
    ) -> None:
        """写 outbox，与业务状态同一事务。

        payload 只放受控 ID 与哈希，不放正文、不放客户信息、不放模型密钥（L1 §7.2）。
        """
        try:
            ordinal = VERSION_EVENT_ORDINAL[event_type]
        except KeyError as exc:
            # 没有登记顺序的事件不得写入：投递器按 (版本, ordinal) 串行，
            # 缺了它就只能凭时间/UUID 猜序，猜错会让撤销先于发布被投递。
            raise ValueError(f"事件类型未登记业务顺序：{event_type}") from exc
        self._conn.execute(
            """INSERT INTO outbox_events
                 (tenant_id, aggregate_type, aggregate_id, aggregate_version, event_type,
                  payload, event_ordinal)
               VALUES (%s, 'knowledge_version', %s, %s, %s, %s, %s)""",
            (self._tenant, aggregate_id, aggregate_version, event_type, Jsonb(payload), ordinal),
        )


__all__ = ["IllegalTransition", "ReviewConflict", "ReviewService", "VersionNotFound", "VersionSnapshot"]
