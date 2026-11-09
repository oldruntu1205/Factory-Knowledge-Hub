# L1 送审：`POST /v1/feedback` 坏例登记（501 → 201）

- 日期：2026-11-09
- 基准：`d883a02`
- 沙箱：`read-only`；**不要探索仓库**，只基于本文事实判断。
- 预算：本切片第 1 次 L1 调用。

## 一、目标

把契约里一直返回 501 的 `POST /v1/feedback` 落成可用能力：
按 `trace_id` 找到本租户已发生的问答，登记一条 `bad_cases`，供后续坏例回归。
**不改数据库 schema**（`bad_cases` 在 `0001` 已存在）。

## 二、契约与实现

请求（OpenAPI 已定义，额外字段禁止）：

```json
{
  "trace_id": "…",
  "category": "WRONG_ANSWER",
  "expected_behavior": {"should_refuse": true}
}
```

处理：

1. `context.require_scope("feedback.create")`；租户只来自令牌。
2. `SELECT id, query_hash FROM rag_queries WHERE tenant_id = %s AND trace_id = %s ORDER BY created_at DESC, id DESC LIMIT 1`。
3. 找不到 / 跨租户 → `404`，响应体固定文案，不回显 trace_id。
4. 插入 `bad_cases`：
   - `query_id` = 命中的 `rag_queries.id`；
   - `category` = 请求 category；
   - `severity` = `'P2'`（固定默认）；
   - `redacted_case` = `{"trace_id", "query_hash", "category"}`，**不含原始 query**；
   - `expected_behavior` = 请求对象；
   - `regression_status` = `'OPEN'`。
5. 返回 `201`：
   `{"bad_case_id", "trace_id", "category", "severity", "regression_status"}`。

OpenAPI 变更：`/v1/feedback` 去掉 501，新增 404/422；请求 schema 不变。

## 三、证据

- `tests/e2e/test_feedback_api.py` 5 条：
  - 无 `feedback.create` → 403；
  - trace 不存在 → 404，响应不回显 trace；
  - 成功 → 201，`bad_cases.query_id` 关联真实 query，`redacted_case` 键只有 `trace_id/query_hash/category`，不含明文 query；
  - 跨租户 trace → 404，不回显 trace；
  - 请求体带 `tenant_id` → 422。
- `pytest tests/e2e/test_feedback_api.py tests/contract/test_api_contract.py` → **17 passed**；ruff/mypy 全绿。
- 未跑全量 suite（本切片只动该端点与 contract yaml；合并前会跑）。

## 四、请 L1 判断

1. **授权范围**：有 `feedback.create` 的人可以按 trace_id 登记**本租户任意主体**的问答，而不只自己的。`redacted_case` 不落原文，只存 query_hash。这个范围是否可接受？还是必须限制 `rag_queries.principal_id = 当前 principal`？
2. **404 语义**：trace 不存在与跨租户都用 404、不回显输入，是否符合"不用错误码探测存在性"？OpenAPI 新增 404 是否足够？
3. **severity 固定 P2**：契约请求没有 severity；当前固定 P2。是否可接受，还是必须按 category 映射/暴露可选 severity？
4. **expected_behavior 原样入库**：调用方可以放任意 JSON（可能含原始 query/PII）。当前只把它当"期望行为"存储，不做脱敏/大小限制（Pydantic dict 无深度/长度上限）。是否阻塞？最小修复是什么？
5. **重复提交**：同一 trace_id 可重复登记多条 bad_case（无唯一约束/幂等键）。是否必须去重？若必须，是否允许加唯一索引（会动 schema）？
6. **outbox**：`event_envelope.json` 列了 `rag.bad_case.created.v1`，当前未发事件。是否本轮必须发，还是可以留到坏例回归接线时？

## 五、固定回复骨架

```text
VERDICT: PASS | CHANGES_REQUESTED

MUST_FIX:
- <问题>: <为什么阻塞> -> <最小修复>

SHOULD_FIX:
- <问题>: <建议>

BOUNDARY:
- <本轮不要求覆盖的项>
```

## 六、边界

- 只审 `/v1/feedback`；`/v1/training/feedback` 仍是 501，不在本轮。
- 不改 `bad_cases` 表结构、不改 RLS、不改鉴权模型。
- 不要探索仓库；测试结果按本文采信。
