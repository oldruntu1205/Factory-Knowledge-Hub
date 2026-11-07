# 审核与发布规格

对应 L1 §3.2、§6.2、§7.2。覆盖"草稿如何变成可对外引用的知识，以及如何撤回"。

## 1. 权限分层

| 端点 | 基础权限 | 额外约束 |
|---|---|---|
| `POST /v1/knowledge/{id}/review` | `knowledge.review` | — |
| `POST /v1/knowledge/{id}/publish`（internal） | `knowledge.publish` | — |
| `POST /v1/knowledge/{id}/publish`（customer_public） | `knowledge.publish` | 追加 `knowledge.publish_public` |
| `POST /v1/knowledge/{id}/revoke` | `knowledge.revoke` | — |

对外发布的额外权限在端点内按 `audience` 分支校验，不放在路由依赖上：同一个路径承载两种
风险等级完全不同的操作，用同一条依赖会把"内部可用"也一起抬高。

## 2. 双人独立审批

`publish_public` 要求 `public_approver ≠ approved_by`，由三层同时保证：

1. **端点**：审批人取自令牌，请求体里没有审批人字段，客户端无法指定"由谁批准"；
2. **服务**：`ReviewService.publish_public` 二次比对；
3. **数据库**：`CHECK (status <> 'PUBLISHED_PUBLIC' OR ... public_approved_by <> approved_by)`。

三层重复是刻意的。前两层能给出可读错误，第三层防止绕过服务直接改库。

## 3. 发布前置条件

- `status = APPROVED_INTERNAL`
- `classification = 0`
- `audience = customer_public`
- `approved_by` 非空

缺任意一项都拒绝，且**不降级**：不会因为审批人不齐就改为"只发内部"。
调用方看到 409，由人决定下一步。

## 4. 撤销的即时性

撤销只做两件事：把状态改为 `REVOKED`，写一条 outbox 事件。

读取侧以状态为准，因此撤销在同一事务提交后立即生效。不等待：

- 向量删除（异步，Phase 2 接入）；
- 缓存清理（Phase 2 接入）；
- 索引 generation 切换。

这是 L1 §7.2 的要求："撤销不能等待事件消费者成功才生效"。代价是索引里会短期残留
已撤销版本的向量，所以检索侧必须有状态过滤——这条在 Phase 2 接入检索时是硬性前提，
不能只依赖向量是否已删除。

## 4.1 弃用（`DISCARDED`）：未对外发布版本的终止路径

2026-11-05 L1 裁决 B（`_evidence/l1_reply_discarded_status_20261105.md`）：
`DRAFT` / `REVIEWED` 没有出口会导致错传文件永久滞留、版本号被占、审核队列堆积。

| 项 | 规则 |
|---|---|
| 允许的来源 | `RAW` / `DRAFT` / `REVIEWED` / `APPROVED_INTERNAL` → `DISCARDED` |
| 禁止的来源 | `PUBLISHED_PUBLIC` / `SUPERSEDED` / `REVOKED`（曾对外可见或已终止，必须走撤销） |
| 动作 | `review_actions.action = DISCARD`，并记录 `from_status`（来源状态） |
| 事件 | `knowledge.version.discarded.v1`（payload 含 `from_status`），一次成功弃用恰好一条 |
| 幂等 | 重复弃用直接返回当前快照，不产生新审计与事件 |
| 终态 | `DISCARDED` 不可再迁移；恢复内容要建新版本 |
| 版本号 | 不回收（分配即不可复用） |
| 审批人 | 保留（从 `APPROVED_INTERNAL` 弃用时，审批是历史事实） |
| 培训 | 引用该版本的已发布课程转 `NEEDS_REVIEW`（与撤销同等待遇）；**已分配学员的三个入口均被阻断**：任务列表不再展示、提交答卷与进度查询都抛错（`TrainingService.my_assignments/submit_attempt/progress`） |
| 权限 | 沿用 `knowledge.revoke`：弃用只收回可用性，不授予发布资格 |

一句话区分：**`REVOKED` = 曾对外可见后撤回；`DISCARDED` = 从未对外可见、主动作废。**

> **终审（2026-11-05）：PASS。** 首轮 CHANGES_REQUESTED 指出「只证明禁止新分配」不足，
> 已补上已分配学员的三个入口阻断并复验通过；回执 `_evidence/l1_reply_discarded_rereview_20261105.md`。
两者互换会让"这份资料是否曾经对外公开过"无法回答，因此审计上不得混用。

## 5. 错误码策略

| 情况 | HTTP | code | 理由 |
|---|---|---|---|
| 版本不存在 | 403 | `ACCESS_DENIED` | 与"无权限"不可区分，防止用错误码探测资源 |
| 跨租户访问 | 403 | `ACCESS_DENIED` | 同上 |
| 状态不符 | 409 | `VERSION_CONFLICT` | 可安全披露"当前状态不允许"，不泄露内容 |
| 双签主体相同 | 409 | `VERSION_CONFLICT` | 同上 |
| UUID 格式错误 | 422 | `VALIDATION_ERROR` | 与资源无关 |

`VersionNotFound` 一律映射成 403 而不是 404。代价是排障时不能只看状态码区分
"真的没有"和"不该看"，需要在服务端日志里看 detail。

## 6. 已知缺口

**未批准内容的出口问题已关闭**（2026-11-05，裁决 B）：`DRAFT` / `REVIEWED` 通过
`DISCARDED` 终止，不再需要留在草稿态；`DRAFT → REVOKED` 仍被拒绝（撤销语义不同）。
决策记录见 [`docs/REVIEW-REQUEST-draft-termination.md`](../REVIEW-REQUEST-draft-termination.md)。

仍然开放的两项：
- **保留期与物理清理**：`DISCARDED` 版本与切片目前只标记不删除（ADR-05 未签）；
  误传客户资料所需的紧急清理期限待业务确认。
- **UI 入口**：当前只有 HTTP 端点 `POST /v1/knowledge/{knowledge_id}/discard`，后台界面未做。

## 7. 验收证据

- `tests/e2e/test_review_flow.py`（10 项）：服务层全链路、双签拒绝、C1 拒绝、
  撤销立即生效、撤销不可复活、outbox 载荷不含敏感字段、跨租户不可见。
- `tests/e2e/test_review_api.py`（8 项）：HTTP 层权限、公开审批额外权限、
  同一人 409、未知版本 403、非法 UUID 422。
- `tests/security/test_state_machine_parity.py`：应用层与数据库状态机一致（状态增至 8 个后自动扩展为 8×8）。
- `tests/e2e/test_discard_flow.py`（10 项）：弃用的允许/拒绝来源、审计与事件、幂等、终态、
  检索不可见、版本号不回收、培训联动、跨租户无副作用。

## 对外发布的三道硬条件（2026-11-07 实测补充）

要把一份内容发布为 `PUBLISHED_PUBLIC`，三个条件缺一不可，且失败都按既有语义返回：

| # | 条件 | 违反时的表现 |
|---|---|---|
| 1 | 版本必须已是 `APPROVED_INTERNAL` | 409（只有内部可用才能对外发布） |
| 2 | **内容分级必须是 C0**（`classification=0`） | 409（`assert_publishable`：仅 C0 可公开） |
| 3 | **公开审批人 ≠ 技术审批人**（四眼原则） | 409（同一主体不得既技术审批又公开审批） |
| 4 | 操作令牌需要 `knowledge.publish_public` | 403 |

另一个容易误判的点：`customer_public` 受众 + `approve_internal` 之后，内容在**客户范围里检索不到**——
可见性谓词要求 `PUBLISHED_PUBLIC + customer_public`（或 `APPROVED_INTERNAL + internal`）。
"批了但没对外发布" 不等于客户能看。
