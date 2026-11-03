"""把检索命中组装成可用于生成的上下文块。

这里承接 L1 §4.4-5 与 §8.4 的 P0：命中 child 后可以恢复 parent 全文，
但必须同时满足「parent 自身可见」与「该 parent 下**全部** child 可见」。

为什么不能只看命中的那一条：parent 文本包含它下面所有 child 的内容。
检索只命中了其中一条，但恢复出来的整段里可能还有另一条对当前主体不可见的句子。
只看命中项，等于用一条可见记录去解锁一整个区块。

判定逻辑复用 `fkh.authz.parent.plan_restore`，本模块只负责取数、去重与预算控制。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import psycopg

from fkh.authz.parent import ChunkAcl, RestorePlan, plan_restore
from fkh.authz.policy import AclFormatError, AclPolicy, Subject
from fkh.domain.chunking.tokenize import ApproxCjkTokenCounter
from fkh.retrieval.lexical import SearchHit

_COUNTER = ApproxCjkTokenCounter()


@dataclass(frozen=True, slots=True)
class ContextBlock:
    chunk_id: UUID
    version_id: UUID
    # True 表示这块是整段恢复的 parent（含未命中的兄弟内容）；False 表示只用命中 child 的原文
    from_parent: bool
    text: str
    tokens: int
    locator: dict[str, Any] | None
    heading_path: tuple[str, ...] | None


@dataclass(frozen=True, slots=True)
class ContextAssembly:
    blocks: tuple[ContextBlock, ...]
    # 因未通过 ACL 而被丢弃的命中数
    denied: int
    # 因预算（块数或 token 上限）被舍弃的块数
    dropped: int
    # 其中整段恢复的 parent 数
    parent_restores: int

    @property
    def tokens(self) -> int:
        return sum(block.tokens for block in self.blocks)


def _policy(row: tuple[Any, ...]) -> AclPolicy | None:
    raw = row[0]
    if raw is None:
        return None
    try:
        return AclPolicy.from_json(raw)
    except AclFormatError:
        # 策略结构损坏等同于"没有策略"：拒绝，而不是猜一个宽松的默认
        return None


def assemble_context(
    conn: psycopg.Connection[Any],
    tenant_id: UUID,
    hits: Sequence[SearchHit],
    subject: Subject,
    *,
    max_blocks: int = 8,
    max_tokens: int = 6000,
) -> ContextAssembly:
    if not hits:
        return ContextAssembly(blocks=(), denied=0, dropped=0, parent_restores=0)
    if max_blocks <= 0 or max_tokens <= 0:
        raise ValueError("max_blocks 与 max_tokens 必须为正数")

    conn.execute("SELECT set_config('app.tenant_id', %s, true)", (str(tenant_id),))

    hit_ids = [hit.chunk_id for hit in hits]
    rows = conn.execute(
        """SELECT id, parent_chunk_id, version_id, chunk_kind, chunk_text,
                  locator, heading_path, acl_policy_id
             FROM knowledge_chunks
            WHERE tenant_id = %s AND id = ANY(%s)""",
        (tenant_id, hit_ids),
    ).fetchall()
    by_id = {UUID(str(row[0])): row for row in rows}
    if not by_id:
        return ContextAssembly(blocks=(), denied=len(hits), dropped=0, parent_restores=0)

    parent_ids = {UUID(str(row[1])) for row in rows if row[1] is not None}

    # 一次取回：父块本身 + 每个父块下的全部子块。分开查会退化成 N+1，
    # 而这里恰恰不能只取"命中的子块"——判定的前提就是完整子块清单。
    siblings: list[tuple[Any, ...]] = []
    if parent_ids:
        siblings = conn.execute(
            """SELECT id, parent_chunk_id, acl_policy_id, chunk_kind
                 FROM knowledge_chunks
                WHERE tenant_id = %s
                  AND (id = ANY(%s) OR parent_chunk_id = ANY(%s))""",
            (tenant_id, list(parent_ids), list(parent_ids)),
        ).fetchall()

    acl_ids = {row[7] for row in rows if row[7] is not None}
    acl_ids |= {row[2] for row in siblings if row[2] is not None}

    policy_by_acl: dict[UUID, AclPolicy | None] = {}
    if acl_ids:
        for acl_row in conn.execute(
            "SELECT id, policy_json FROM acl_policies WHERE tenant_id = %s AND id = ANY(%s)",
            (tenant_id, list(acl_ids)),
        ).fetchall():
            policy_by_acl[UUID(str(acl_row[0]))] = _policy((acl_row[1],))

    def to_acl(chunk_id: UUID, parent_id: Any, acl_id: Any) -> ChunkAcl:
        policy = policy_by_acl.get(UUID(str(acl_id))) if acl_id is not None else None
        return ChunkAcl(
            chunk_id=str(chunk_id),
            tenant_id=str(tenant_id),
            parent_chunk_id=str(parent_id) if parent_id is not None else None,
            policy=policy,
        )

    hit_acls = {
        UUID(str(row[0])): to_acl(UUID(str(row[0])), row[1], row[7]) for row in rows
    }

    children_by_parent: dict[str, list[ChunkAcl]] = {}
    parents: dict[str, ChunkAcl] = {}
    parent_text: dict[str, tuple[str, dict[str, Any] | None, tuple[str, ...] | None, UUID]] = {}

    for row in siblings:
        sid, sparent, sacl, _kind = UUID(str(row[0])), row[1], row[2], row[3]
        if sparent is None:
            parents[str(sid)] = to_acl(sid, None, sacl)
        else:
            children_by_parent.setdefault(str(sparent), []).append(to_acl(sid, sparent, sacl))

    if parent_ids:
        for row in conn.execute(
            """SELECT id, chunk_text, locator, heading_path, version_id
                 FROM knowledge_chunks
                WHERE tenant_id = %s AND id = ANY(%s)""",
            (tenant_id, list(parent_ids)),
        ).fetchall():
            parent_text[str(row[0])] = (str(row[1]), dict(row[2]) if row[2] else None,
                                        tuple(row[3]) if row[3] else None, UUID(str(row[4])))

    plan: RestorePlan = plan_restore(
        [hit_acls[hit.chunk_id] for hit in hits if hit.chunk_id in hit_acls],
        children_by_parent,
        parents,
        subject,
    )
    restorable = set(plan.restore_parents)
    # plan_restore 内部的 chunk_id 是字符串（与 ChunkAcl 构造时一致），
    # 这里必须同样比较字符串；用 UUID 去匹配 Collection[str] 会恒为 False，
    # 结果是被拒绝的命中悄悄进入上下文。
    denied_ids = set(plan.denied)

    blocks: list[ContextBlock] = []
    used_parents: set[str] = set()
    denied = 0
    dropped = 0
    parent_restores = 0
    budget = max_tokens

    for hit in hits:  # hits 已按 score 降序
        hit_row = by_id.get(hit.chunk_id)
        if hit_row is None or str(hit.chunk_id) in denied_ids:
            denied += 1
            continue

        parent_id = str(hit_row[1]) if hit_row[1] is not None else None
        use_parent = parent_id is not None and parent_id in restorable

        block_text: str
        block_locator: dict[str, Any] | None
        block_heading: tuple[str, ...] | None
        block_version: UUID

        if use_parent and parent_id is not None:
            if parent_id in used_parents:
                continue  # 同一父块的多个命中合并成一块
            parent_row = parent_text[parent_id]
            block_text = parent_row[0]
            block_locator = parent_row[1]
            block_heading = parent_row[2]
            block_version = parent_row[3]
            chunk_id = UUID(parent_id)
        else:
            # 同一父块下不同 child 各自成块，不能按 parent 去重——它们是不同内容
            block_text = str(hit_row[4])
            block_locator = dict(hit_row[5]) if hit_row[5] else None
            block_heading = tuple(hit_row[6]) if hit_row[6] else None
            block_version = UUID(str(hit_row[2]))
            chunk_id = hit.chunk_id

        tokens = _COUNTER.count(block_text)
        if len(blocks) >= max_blocks or tokens > budget:
            dropped += 1
            continue
        budget -= tokens
        if use_parent:
            used_parents.add(str(parent_id))
            parent_restores += 1

        blocks.append(
            ContextBlock(
                chunk_id=chunk_id,
                version_id=block_version,
                from_parent=use_parent,
                text=block_text,
                tokens=tokens,
                locator=block_locator,
                heading_path=block_heading,
            )
        )

    return ContextAssembly(
        blocks=tuple(blocks), denied=denied, dropped=dropped, parent_restores=parent_restores
    )
