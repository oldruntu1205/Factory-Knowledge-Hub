VERDICT: CHANGES_REQUESTED

MUST_FIX:
- 请求体上限未落实：本文要求沿用 16 KiB，但新路径尚未加入路径表，现有通过证据不足以证明该约束生效 -> 将 `/v1/training/feedback` 加入 `_BODY_LIMITS`，复用 `/v1/feedback` 的限制及超限响应；补一条超限请求被拒绝且不写入 `bad_cases` 的回归测试。

SHOULD_FIX:
- 正向 trace 覆盖：补“本人任务 + 本人 internal_training trace → 201”，验证 `query_id` 关联正确，并核对 `query_hash` 来源。
- 拒绝分支覆盖：补课程非 `PUBLISHED` → 409、任务不存在 → 403、trace 不存在/跨租户/非培训 scope → 404，并验证拒绝时不落库。
- 封闭结构验证：确认新路由实际复用了 `/v1/feedback` 的校验模型；建议补未知字段及非法 `expected_status` → 422 的路由测试。
- 状态码口径：所述 403/409/404 区分合理，尤其应保持先校验任务归属、再判断课程状态；仅凭本文不能确认与其他既有培训端点完全一致。

BOUNDARY:
- 接受复用 `bad_cases`：当前目标是结构化坏例登记，`TRAINING_FEEDBACK` 分类及 assignment/course/version 上下文足够，本轮不要求新表或 schema 变更。
- 接受 trace 可选：无 trace 时作为课程/任务级反馈，`query_id=NULL`；不要求定位具体问答。
- 接受仅 `learner` + `internal_training`，并强制本人任务、本人培训问答；不要求导师或管理员代提交。
- 按本文提供的契约，接受不发 outbox 事件及同一任务重复反馈；不要求新增事件、去重或幂等机制。
- 本结论仅基于送审材料；未探索仓库，未独立复跑所述检查。