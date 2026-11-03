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

**未批准内容没有出口**：`DRAFT` 与 `REVIEWED` 都不能直接转 `REVOKED`，
上传错误的文件只能一直留在草稿态。这是 L1 §3.2 未定义的部分，
方案与影响见 [`docs/REVIEW-REQUEST-draft-termination.md`](../REVIEW-REQUEST-draft-termination.md)。

裁决前不得擅自往状态机里加迁移——`tests/e2e/test_review_flow.py::test_draft_cannot_be_revoked_pending_adr`
固定了当前行为。

## 7. 验收证据

- `tests/e2e/test_review_flow.py`（10 项）：服务层全链路、双签拒绝、C1 拒绝、
  撤销立即生效、撤销不可复活、outbox 载荷不含敏感字段、跨租户不可见。
- `tests/e2e/test_review_api.py`（8 项）：HTTP 层权限、公开审批额外权限、
  同一人 409、未知版本 403、非法 UUID 422。
- `tests/security/test_state_machine_parity.py`（46 项）：应用层与数据库状态机一致。
