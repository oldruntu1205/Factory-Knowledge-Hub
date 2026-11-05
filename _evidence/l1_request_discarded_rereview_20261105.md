# L1 复审：弃用后「已分配学员」访问阻断（`9fa3865`）

- 日期：2026-11-05
- 对应：`_evidence/l1_reply_discarded_final_20261105.md`（CHANGES_REQUESTED 1 条）
- 请只做判断：**不要探索仓库、不要读源码、不要跑测试**。

## 1. 你的质疑成立（先说结论）

`TrainingService.assign()` 抛错确实只证明「不能新增分配」。复核后发现这是**真实缺口**：
`my_assignments` / `submit_attempt` / `progress` 三个学员入口此前都**不检查课程状态**，
课程因知识被撤销/弃用转 `NEEDS_REVIEW` 后，已分配学员仍可提交答卷、查进度——
这个缺口同样存在于旧的「撤销知识 → 课程 NEEDS_REVIEW」路径，不只是本次弃用。

## 2. 改了什么（`src/fkh/domain/training.py`）

| 学员入口 | 改动 |
|---|---|
| `my_assignments()` | 只返回 `c.status = 'PUBLISHED'` 的课程；作废课程不再出现在学员任务列表 |
| `submit_attempt()` | 读取分配时一并取课程状态；非 `PUBLISHED` → `TrainingError("课程当前不可用（NEEDS_REVIEW），暂停提交")` |
| `progress()` | 同样取课程状态；非 `PUBLISHED` → `TrainingError(...进度查询暂停)` |

未改动：已提交的答卷与历史成绩**保留可查权限归审计/运营侧**（不删数据、不改历史），
学员侧只做"入口拒绝"。

## 3. 证据（同一个用例内完成"弃用前能学 → 弃用后不能学"的对照）

`tests/e2e/test_discard_flow.py::test_discard_marks_course_and_blocks_enrolled_learner`：

1. 建课（引用该知识版本）→ 发题 → 发布 → **分配学员 A** → 提交答卷成功 → `my_assignments` 返回 1 条；
2. 弃用该知识版本 → 课程自动转 `NEEDS_REVIEW`；
3. 断言三个入口全部拒绝：
   - `my_assignments(learner) == []`（列表不再展示）
   - `submit_attempt(...)` 抛 `TrainingError`（不能继续考试）
   - `progress(...)` 抛 `TrainingError`（不能查进度）
4. 断言同时覆盖"弃用前成功"与"弃用后拒绝"，不是只测拒绝分支。

## 4. 门禁（独家运行）

```
uv run pytest -q                      → 403 passed, 26.92s
uv run pytest -q -m requires_pg       → 254 passed, 149 deselected, 0 skip
uv run ruff check src tests migrations → All checks passed
uv run mypy                           → Success: no issues found in 51 source files
```

## 5. 能力边界（明确写清，不含糊）

- 本轮阻断的是**学员侧三个入口**；运营/审核侧仍可查看历史分配与成绩（审计需要）。
- 课程因 `REVOKED` 转 `NEEDS_REVIEW` 时同样被阻断——本修复覆盖两条路径，不只弃用。
- 物理清理、保留期限、后台 UI 入口仍按你的裁决延后（ADR-05 / 业务确认）。

请裁决：**PASS** / **仍 CHANGES_REQUESTED（仅列未满足点）**。
