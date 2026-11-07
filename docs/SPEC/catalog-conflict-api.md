# 目录冲突待办 HTTP 接口（2026-11-07）

> 依据：`_evidence/l1_reply_conflict_api_20261107.md`（有条件通过，条件已落实）
> 用途：业务前端第一片（冲突待办页）的前置接口；此前只有 CLI。

## 权限

| 端点 | scope | 主体 |
|---|---|---|
| `GET /v1/catalog/conflicts` | `catalog.read` | 具备读取权限的主体（不额外禁止 service/agent） |
| `POST /v1/catalog/conflicts/{group_id}/resolve` | `catalog.read` + `catalog.resolve` | **必须是该租户活跃 user 主体**（服务端 `require_operator` 复核，不信签发时状态） |

新增的两个 scope 由令牌签发侧登记；**既有令牌不会自动获得新权限**。

## 可见性（决定谁能看到哪一组）

按顺序判定，任一不满足即不可见：

1. 租户匹配（RLS + 显式条件）；
2. 版本状态在 `OPERABLE_STATUSES`（`RAW/DRAFT/REVIEWED/APPROVED_INTERNAL/PUBLISHED_PUBLIC`）；
3. 版本的 `required_scope` 在令牌 `allowed_scopes` 内；
4. **该组所有候选 chunk 的 ACL 都允许**——只要有一条候选被拒，整组隐藏，
   否则复核人员会在"缺一条价格"的对照表上做决议。

**过滤发生在计数与分页之前**：`total` 是过滤后的可见数量；隐藏组不占分页位置。

## 错误口径

| 场景 | 响应 |
|---|---|
| 未认证 / 令牌无效 | 401 |
| 缺 scope、POST 非 user 主体、主体已停用 | 403 `ACCESS_DENIED` |
| 不存在 / 跨租户 / scope 不符 / ACL 拒绝 | **404 `NOT_FOUND`**（同一响应，不给探测面） |
| 已决议（重复提交、并发后到者） | 409 `VERSION_CONFLICT`，且不改写任何决议字段 |
| 版本已撤销 / 弃用 / 被替代 | 409 `VERSION_CONFLICT`（**在访问获准之后**才判，不能提前藏成 404） |
| KEEP 未给候选 / 候选不属于该组 / DROP_ALL 携带 `candidate_id`（含显式 null） | 422 `VALIDATION_ERROR` |

`NOT_FOUND` 是本次新增的错误码（`error.json` + `errors.py`），错误体仍走统一结构。

## 响应

列表：`{total, limit, offset, items[]}`；单组：`{group_id, version_id, code, status, note,
resolved_by, resolved_at, selected_candidate_id, candidates[]}`。

- **不含原文整行**（`raw_text`）：只给 `spec / raw_price / locator`；
- `selected_candidate_id`：`KEEP` 时为所选候选，`DROP_ALL`/`OPEN` 时为 null——
  只有 `KEEP` 状态无法说明保留了谁；
- 排序 `code ASC, group_id ASC`（code 不唯一，必须补唯一键）；
- 分页 `limit` 1–200（默认 50）、`offset` ≥ 0；越界返回空列表但 `total` 保持真实可见值。

## 决议语义（沿用既有裁决）

同一事务内：锁组 → 校验 OPEN → 校验候选归属 → 校验版本可操作 → 回填条目 → 落决议。
一次性生效、不可原地改判（错判走新版本）。并发时先到者赢，后到者 409。

**终审：PASS**（`_evidence/l1_reply_conflict_api_final_20261107.md`）——有条件通过的各项条件已落实；
性能、一致性、业务记录与 ACL 限制列为已披露边界。

## 已知限制

- 列表为"全量过滤后再切片"：租户内组数很大时是 O(n) 内存过滤（本片不承诺浏览期间快照一致性，
  也不做游标分页）；
- 组行记录决议（状态/操作者/时间/备注/所选候选），**不构成独立防篡改审计日志**；
- 本片不做改判、批量决议、认领（claim）。
