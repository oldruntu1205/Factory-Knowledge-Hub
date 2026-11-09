# L1 复送：培训反馈请求体上限与拒绝分支（回应 CHANGES_REQUESTED）

- 日期：2026-11-09
- 上一轮回执：`_evidence/l1_reply_training_feedback_20261109.md`
- 预算：本切片第 2 次 L1 调用。

## 一、MUST_FIX 修复

`main.py` 的 `_BODY_LIMITS` 已加入培训反馈：

```python
_BODY_LIMITS = {
    "/v1/knowledge/documents": 8 * 1024 * 1024,
    "/v1/feedback": 16 * 1024,
    "/v1/training/feedback": 16 * 1024,
}
```

- 与 `/v1/feedback` 共用接收层累计限额与 413 响应；
- 新增 `test_training_feedback_rejects_oversized_body`：16 KiB + 1 字节 → 413，
  并直接查 `bad_cases` 计数，确认**拒绝请求不落库**。

## 二、SHOULD_FIX 补充

- **正向 trace**：`test_training_feedback_links_own_trace`——本人培训 ask 产生的 trace
  → 201；核对 `bad_cases.query_id` 等于 `rag_queries.id`，`redacted_case.query_hash`
  等于该 trace 的 `query_hash`。
- **拒绝分支**：
  - 任务不存在 → 403（`test_training_feedback_rejects_nonexistent_assignment_and_bad_body`）；
  - 课程撤销后 → 409（`test_training_feedback_rejects_unavailable_course`）；
  - 他人 trace → 404（已有 `test_training_feedback_rejects_other_learners_trace`）；
  - 缺 `internal_training` 范围 → 403（已有）。
- **封闭结构**：非法 `expected_status="MAYBE"` → 422；顶层额外 `tenant_id` → 422。
- 新路由复用 `TrainingFeedbackRequest.expected_behavior: ExpectedBehavior`，与 `/v1/feedback`
  同一 Pydantic 模型，未复制校验逻辑。

定向：`pytest tests/e2e/test_training_flow.py tests/contract/test_api_contract.py` → **32 passed**；
ruff/mypy 全绿。

## 三、请 L1 判断

1. `_BODY_LIMITS` 加入 `/v1/training/feedback` + 413 不落库回归，是否解除 MUST_FIX？
2. 正向 trace 关联与 403/409/404/422 拒绝分支是否足以 PASS？
3. 若仍有 MUST_FIX，请给最小修复；否则请 PASS。

## 四、固定回复骨架

```text
VERDICT: PASS | CHANGES_REQUESTED

MUST_FIX:
- <问题>: <为什么阻塞> -> <最小修复>

SHOULD_FIX:
- <问题>: <建议>

BOUNDARY:
- <本轮不要求覆盖的项>
```
