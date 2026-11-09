# L1 送审：`POST /v1/training/feedback` 培训反馈（501 → 201）

- 日期：2026-11-09
- 基准：`5c37464`（通用 `/v1/feedback` 已 PASS）
- 沙箱：`read-only`；**不要探索仓库**，只基于本文事实判断。
- 预算：本切片第 1 次 L1 调用。

## 一、目标与边界

把培训契约里最后一条 501（`POST /v1/training/feedback`）落成受控坏例登记：
学员对**本人**课程/问答提交结构化反馈，写入已有 `bad_cases`；**不改 schema**。
`/v1/feedback` 已实现通用版本；本轮只加培训绑定与本人校验。

## 二、契约

```json
{
  "assignment_id": "…",
  "trace_id": "…",                       // 可选
  "expected_behavior": {
    "expected_status": "REFUSED",        // ANSWERED/REFUSED/HUMAN_REVIEW_REQUIRED
    "must_cite": true
  }
}
```

- `expected_behavior` 复用 `/v1/feedback` 的封闭结构；无自由文本/未知字段。
- 请求体沿用全局 16 KiB 上限（`/v1/feedback` 路径表；`/v1/training/feedback` 目前未单独列入 16 KiB 表——见下方问题 5）。

## 三、处理

1. `context.require_scope("learner")`，且 `allowed_scopes` 必须含 `internal_training`；否则 403。
2. `TrainingService.assert_can_ask(assignment_id, context.subject)`：
   - 任务不存在/不属于本人 → 统一 403（不泄露存在性）；
   - 课程非 `PUBLISHED` → 409（任务暂停，不是权限问题）。
3. `trace_id` 可选：
   - 不传：`query_id=NULL`，只记课程/任务上下文；
   - 传了：查 `rag_queries`（tenant + trace），必须 `principal_id == 当前 principal` 且 `scope == internal_training`，否则 404（不泄露他人问答是否存在）。
4. 写 `bad_cases`：
   - `category='TRAINING_FEEDBACK'`，`severity='P2'`；
   - `redacted_case = {kind, assignment_id, course_id, course_version, trace_id, query_hash}`；
   - `expected_behavior` 原样（封闭结构）；`regression_status='OPEN'`。
5. 返回 201：`bad_case_id/assignment_id/trace_id/category/severity/regression_status`。

OpenAPI：`/v1/training/feedback` 去掉 501，补 requestBody、404、409、422。

## 四、证据

`tests/e2e/test_training_flow.py` 新增 4 条：
- 本人任务 + 无 trace → 201，`bad_cases` 关联 assignment/course，`trace_id=NULL`；
- 他人任务 → 403；
- 用他人培训问答 trace → 404，响应不回显 trace；
- 缺 `internal_training` 范围 → 403。

定向：`pytest tests/e2e/test_training_flow.py tests/contract/test_api_contract.py` → **28 passed**；ruff/mypy 全绿。

## 五、请 L1 判断

1. **复用 `bad_cases` 承载“课程反馈”**是否可接受？当前没有独立 `training_feedback` 表；`category='TRAINING_FEEDBACK'` + `redacted_case` 记录 assignment/course/version/trace。还是必须新建表（会动 schema）？
2. **trace_id 可选**：不传时只有课程/任务上下文，无法回溯到具体问答；是否可接受？
3. **403/409/404 语义**：任务不存在/非本人 403，课程不可用 409，trace 非本人/不存在 404；是否符合既有培训端点口径？
4. **权限**：仅 `learner` + `internal_training`；导师/管理员不能代提交。是否足够？
5. **请求体上限**：`/v1/training/feedback` 未加入 16 KiB 路径表（当前只有 `/v1/feedback`）。是否必须同一上限？最小修复是把该路径加入 `_BODY_LIMITS`。
6. **outbox/重复**：不发 `rag.bad_case.created.v1`；同一任务允许重复反馈。是否可接受？

## 六、固定回复骨架

```text
VERDICT: PASS | CHANGES_REQUESTED

MUST_FIX:
- <问题>: <为什么阻塞> -> <最小修复>

SHOULD_FIX:
- <问题>: <建议>

BOUNDARY:
- <本轮不要求覆盖的项>
```
