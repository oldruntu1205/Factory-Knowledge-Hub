# 反馈与坏例登记（`POST /v1/feedback`）

- 日期：2026-11-09
- 状态：已实现（L1 PASS），`/v1/training/feedback` 仍是 501。

## 1. 契约

```json
{
  "trace_id": "trace-...",
  "category": "WRONG_ANSWER",
  "expected_behavior": {
    "expected_status": "REFUSED",
    "must_cite": false
  }
}
```

| 字段 | 约束 |
|---|---|
| `trace_id` | 1–128 字符；必须是本租户已发生的 `rag_queries.trace_id` |
| `category` | 1–64 字符；自由分类标签（不参与授权） |
| `expected_behavior` | 封闭对象：`expected_status` ∈ `ANSWERED/REFUSED/HUMAN_REVIEW_REQUIRED`，`must_cite` 布尔；**不接受自由文本/未知字段** |

请求体上限 **16 KiB**（接收层累计，chunked 也不能绕过）；超限 413。

## 2. 权限与可见性

- 需要 `feedback.create`。
- 这是**租户级登记权限**：可关联本租户任意主体的问答，不限制为本人。
- 租户来自令牌；跨租户 trace 与不存在的 trace 都返回 **404**，响应不回显 trace_id。
- `query_hash` 不是匿名化，不能作为扩大授权的理由。

## 3. 落库

写入 `bad_cases`：

| 列 | 值 |
|---|---|
| `query_id` | 命中的 `rag_queries.id` |
| `category` | 请求 category |
| `severity` | 固定 `P2` |
| `redacted_case` | `{"trace_id", "query_hash", "category"}`，不含原始 query |
| `expected_behavior` | 请求的封闭对象 |
| `regression_status` | `OPEN` |

同一 trace 允许重复登记（多人/多角度反馈），不加唯一索引或幂等键。

## 4. 未完成

- `rag.bad_case.created.v1` outbox 事件尚未接线；坏例回归消费端/告警另立切片。
- `/v1/training/feedback` 仍是 501，需要单独的培训反馈契约（本轮未定义）。
- 未做坏例状态流转（FIXING/FIXED/WONTFIX）与回归看板。

## 5. 验证

```bash
uv run pytest -q tests/e2e/test_feedback_api.py tests/contract/test_api_contract.py
```

覆盖：无权限 403、不存在/跨租户 404、成功 201 且只落脱敏引用、未知字段/自由文本 422、
16 KiB 超限 413、chunked 超限 413、恰好 16 KiB 不被限流误杀。
