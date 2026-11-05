# L1 送审稿：知识版本终止路径（DRAFT / REVIEWED 无出口）

- 日期：2026-11-05
- 提出方：L2 实施
- 风险归类命中：**状态机变更**（应用层白名单 + 数据库触发器 + 迁移）、**对外事件契约**、**审计语义**
- 前置材料：`docs/REVIEW-REQUEST-draft-termination.md`（2026-11-03 提出，仍未裁决）
- 请只做判断：**不要探索仓库、不要读其他文件、不要跑测试**。

## 1. 事实（已取证）

**状态机**（7 状态，应用层 `domain/state_machine.py` 与数据库触发器
`fkh_guard_version_mutation` **各写一份白名单**，另有 7×7 全组合对拍测试保证两层一致）：

```
RAW → DRAFT
DRAFT → REVIEWED
REVIEWED → DRAFT            （退回）
REVIEWED → APPROVED_INTERNAL
APPROVED_INTERNAL → DRAFT   （退回）
APPROVED_INTERNAL → PUBLISHED_PUBLIC
APPROVED_INTERNAL → SUPERSEDED
APPROVED_INTERNAL → REVOKED
PUBLISHED_PUBLIC → SUPERSEDED
PUBLISHED_PUBLIC → REVOKED
SUPERSEDED → REVOKED
```

`DRAFT` 与 `REVIEWED` **没有任何终止路径**；`REVIEWED` 退回成的 `DRAFT` 同样出不去。

**后果**（已由业务侧确认）：
1. 传错文件（拿错型号 / 传了别的客户资料）后，该草稿永久滞留；
2. `version_no` 被占用且持续递增，版本号与实物文档对不上；
3. 该版本切片仍在 `knowledge_chunks`（检索靠状态过滤挡住，但存储与统计开销都在）；
4. 审核队列堆积永远不会处理的草稿。

**当前实现**：`REVIEWED` 也不能直接 `REVOKED`；`DRAFT` 调撤销会被应用层拒绝。
测试 `tests/e2e/test_review_flow.py::test_draft_cannot_be_revoked_pending_adr`
**固定了当前行为**（防止有人"顺手"加一条绕过裁决），改动时该用例要随裁决改写。

**受影响的下游**：
- `review_actions.action` 是 CHECK 枚举（`SUBMIT / REVIEW_PASS / REVIEW_REJECT / APPROVE_INTERNAL /
  APPROVE_PUBLIC / PUBLISH / SUPERSEDE / REVOKE`）——新增动作要改约束；
- `contracts/schema/event_envelope.json` 的 `event_type` 是封闭 enum（15 个），**没有** discarded 事件；
- 培训联动触发器**只在 `REVOKED` 时**把引用该版本的已发布课程转为 `NEEDS_REVIEW`；
- 检索可见性硬条件为 `status IN ('APPROVED_INTERNAL','PUBLISHED_PUBLIC')`，新增状态天然不可见；
- 版本内容不可变（触发器强制），"作废"只能靠状态。

**迁移编号约束**：`0000–0009`、`0011`、`0012` 已应用；`0010` 是草稿终止原预留号，
但按 2026-11-04 的裁决「允许跳号、**禁止倒序回填**」，`apply.py` 会拒绝把 0010 补在 0012 之后。
**因此本切片只能取 `0013`。**

## 2. 候选方案（沿用 2026-11-03 审查请求，补充实施细节）

- **A. 允许 `DRAFT → REVOKED`**：改动最小；但 `REVOKED` 同时表示「曾发布后被撤回」与
  「从未发布只是写错」，审计上无法回答"这份内容是否曾经对外可见"。
- **B. 新增 `DISCARDED`（从未发布、主动作废）**：语义唯一。需要：状态枚举 + CHECK + 白名单
  + 新动作 `DISCARD` + 新事件 `knowledge.version.discarded.v1` + 培训联动 + 迁移 0013。
- **C. 软删除列 `discarded_at`**：不动状态机，状态与"是否作废"变成两个维度，任何一条查询漏掉
  条件就会泄露，风险偏高。

## 3. 待判问题（请逐条裁决）

1. **A / B / C 选哪个？**（实施侧建议 B，理由同上：审计唯一性。）
2. **若选 B，哪些源状态允许 → `DISCARDED`？** 实施侧建议：`DRAFT`、`REVIEWED`、
   `APPROVED_INTERNAL` 三个；**`PUBLISHED_PUBLIC` / `SUPERSEDED` 不允许**（内容曾对外可见，
   必须走 `REVOKED`，否则"是否公开过"再次失真）。请确认或修正。
3. **`RAW` 能否直接 → `DISCARDED`？**（上传即发现错误，还没进 DRAFT。）
4. **从 `APPROVED_INTERNAL` 弃用时，`approved_by` 是否保留？** 现有触发器只禁止
   `RAW/DRAFT` 携带审批人；`DISCARDED` 若保留审批人，语义是"批准过但从未发布"。请裁决。
5. **新事件 `knowledge.version.discarded.v1`**：是否新增到 `event_envelope.json` 的 enum？
   目前仓库内没有事件消费方（relay 只负责投递），但 enum 是对外契约。
6. **培训课程联动**：引用被弃用版本的已发布课程，是否同样转 `NEEDS_REVIEW`？
   （现有只有 `REVOKED` 触发。）实施侧建议：**要**，与撤销同等待遇——学员不应继续学作废内容。
7. **保留期限**（ADR-05）：弃用版本与切片何时物理清理？本轮是否可以先"只标记不清理"？
8. **`version_no` 是否回收？** 实施侧建议不回收（分配即不可复用，否则引用会串）。
9. **`DISCARDED` 是否终态？** 实施侧建议是（不可复活；要恢复就建新版本）。
10. **审批要求**：弃用是否需要双签？（撤销当前是单主体；发布才要求双签。）

## 4. 实施侧计划（供裁决参考，不预设结论）

- 迁移 `0013_discarded_status.sql`：`knowledge_versions.status` CHECK 增加 `DISCARDED`；
  触发器白名单增加对应边；`review_actions.action` CHECK 增加 `DISCARD`；回滚脚本反向收紧。
- 应用层：`VersionStatus.DISCARDED`、`ReviewAction.DISCARD`、`ReviewService.discard()`；
  `TERMINAL_STATUS` 目前是 `REVOKED` 单值，若 `DISCARDED` 也是终态，需要改成集合或加判定函数。
- 事件：按裁决增删；培训联动按裁决增删。
- 断言（若选 B）：
  1. 三个源状态的合法/非法组合与数据库触发器**逐对一致**（对拍从 7×7 扩到 8×8）；
  2. `DISCARDED` 不可再迁移（终态）；
  3. 弃用写 `review_actions(action=DISCARD)` 且只写一次；
  4. 弃用时 `outbox` 事件恰好一条，类型符合裁决；
  5. 检索与证据端点**永不返回**被弃用版本（现有状态过滤 + 新增断言）；
  6. `version_no` 不被回收（新版本号继续递增）；
  7. 培训联动（若采纳）在弃用后触发课程 `NEEDS_REVIEW`；
  8. 跨租户弃用返回 403（与撤销同策略）；
  9. `PUBLISHED_PUBLIC → DISCARDED` 必须被拒绝（若采纳 §3.2 建议）。

## 5. 边界与预算

- 只做判断：不探索仓库、不写代码、不跑测试；一次调用回完 §3 的 1–10。
- 回复骨架：`问题号 → 裁决 + 一句理由 + 需要的断言`；不确定的标注「待业务确认」。
- 本切片 L1 调用计划：本次（裁决）+ 实现后终审，共 2 次。
