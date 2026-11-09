# 反馈与坏例登记（`/v1/feedback` + `/v1/training/feedback`）

- 日期：2026-11-09
- 状态：两个端点均已实现（L1 PASS）。

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

## 4. 培训反馈（`POST /v1/training/feedback`）

```json
{
  "assignment_id": "…",
  "trace_id": "…",
  "expected_behavior": {
    "expected_status": "REFUSED",
    "must_cite": true
  }
}
```

- 权限：`learner` + `allowed_scopes` 含 `internal_training`。
- `assignment_id` 必须是**本人**任务（他人/不存在统一 403，不泄露存在性）；
  课程非 `PUBLISHED` → 409。
- `trace_id` 可选；传了必须是**本人**、`internal_training` 范围的问答，否则 404。
- 落 `bad_cases`：`category='TRAINING_FEEDBACK'`，`severity='P2'`，
  `redacted_case` 记录 assignment/course/version/trace/query_hash，不含原文。
- 请求体上限同为 **16 KiB**；超限 413 且不落库。
- 同一任务允许重复反馈；不发 outbox 事件。

## 5. 未完成

- `rag.bad_case.created.v1` outbox 事件尚未接线；坏例回归消费端/告警另立切片。
- 未做坏例状态流转（FIXING/FIXED/WONTFIX）与回归看板。
- 培训反馈暂无独立课程反馈表/评分/自由文本（按 L1 决定只收封闭结构）。

## 5. 验证

```bash
uv run pytest -q tests/e2e/test_feedback_api.py tests/e2e/test_training_flow.py tests/contract/test_api_contract.py
```

通用端点覆盖：无权限 403、不存在/跨租户 404、成功 201 且只落脱敏引用、未知字段/自由文本 422、
16 KiB 超限 413、chunked 超限 413、恰好 16 KiB 不被限流误杀。

培训端点覆盖：本人任务 201、他人任务 403、课程撤销 409、他人 trace 404、
缺培训范围 403、非法枚举/额外字段 422、超限 413 且不落库、本人 trace 的 query_id/query_hash 关联。
