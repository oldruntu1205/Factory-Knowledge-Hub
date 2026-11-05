# L1 终审：版本作废（`DISCARDED`）实现（`b186bb0`）

- 日期：2026-11-05
- 对应裁决：`_evidence/l1_reply_discarded_status_20261105.md`（方案 B + 10 条问答 + 实施约束）
- 请只做判断：**不要探索仓库、不要读源码、不要跑测试**。

## 1. 实现摘要

| 层 | 改动 |
|---|---|
| 状态机 | `VersionStatus.DISCARDED`；`ReviewAction.DISCARD`；白名单新增 `RAW/DRAFT/REVIEWED/APPROVED_INTERNAL → DISCARDED`；`TERMINAL_STATUSES = {REVOKED, DISCARDED}` |
| 服务 | `ReviewService.discard()`：`FOR UPDATE` 行锁 → 幂等短路 → 状态更新 → `review_actions(action=DISCARD, from_status=来源状态)` → outbox 事件 → 同一事务 |
| HTTP | `POST /v1/knowledge/{knowledge_id}/discard`，权限沿用 `knowledge.revoke`（裁决 Q10） |
| 迁移 0013 | 状态 CHECK（+DISCARDED）、触发器白名单（+4 边）、`review_actions.action` CHECK（+DISCARD）、新增 `review_actions.from_status`（历史行 NULL）、培训联动扩展到 DISCARDED |
| 事件 | `knowledge.version.discarded.v1`（ordinal=3，payload 含 `from_status`），加入 `event_envelope.json` enum |
| 回滚 | `0013_down.sql` 先检查 `DISCARDED` 版本与 `DISCARD` 审计，存在则 `RAISE` 拒绝；`apply.py` 把这类拒绝转成可读 `SystemExit`（不甩 traceback），并整体中止、不做任何变更 |

## 2. 断言映射（裁决要求的 10 条）

| # | 要求 | 证据 |
|---|---|---|
| 1 | 状态、动作、白名单、数据库约束一致；原子写入状态+审计+事件 | `tests/e2e/test_discard_flow.py::test_discard_from_draft_writes_status_audit_and_single_event`（状态/审计/事件同一事务、单条）；`tests/security/test_state_machine_parity.py` 随枚举自动扩到 **8×8** |
| 2 | 允许四个来源；拒绝 PUBLISHED_PUBLIC/SUPERSEDED/REVOKED；保留 `DRAFT→REVOKED` 被拒并另测可弃用 | `test_discard_from_reviewed_and_approved_internal`、`test_published_version_cannot_be_discarded`、`test_revoked_and_superseded_cannot_be_discarded`；`test_review_flow.py::test_draft_cannot_be_revoked_but_can_be_discarded`（两条断言都保留） |
| 3 | `RAW→DISCARDED` 成功；后台任务不得把已弃用版本推进/继续切片 | 状态机允许该边（对拍覆盖）；`test_discarded_is_terminal` 中 `pass_review/approve/publish/revoke` 全部被拒。入库是**单事务**（解析→切片→落库→RAW→DRAFT 同事务），不存在"弃用后又被后台推进"的窗口——这一点是结构性的，不是靠测试兜底 |
| 4 | `approved_by` 保留、不补写、操作者独立记录 | 同 #2 的 APPROVED_INTERNAL 用例断言 `approved_by` 不变；审计 `actor_id` 另有断言 |
| 5 | 新事件入 enum、过 schema 校验、一次成功一条、重复不重复 | `test_..._single_event` 用 `Draft202012Validator` 校验实际 outbox 行；`test_repeated_discard_is_idempotent` 断言审计与事件各仍为 1 条 |
| 6 | 培训课程转 NEEDS_REVIEW；**验证学员访问被阻断**（不只看状态字段） | `test_discard_marks_referencing_course_needs_review`：课程转 `NEEDS_REVIEW` 后，`TrainingService.assign()` 抛 `TrainingError`（分配是学员获得课程的入口）；只影响本租户引用该版本的课程（SQL 条件 + 复合外键） |
| 7 | 本轮只标记不物理清理 | 未写任何 DELETE；`DISCARDED` 只影响状态与可见性 |
| 8 | 版本号不回收 | `test_version_number_is_not_reused_after_discard`：弃用后新版本 `version_no = 2` |
| 9 | 终态、不可复活、重复弃用幂等 | `test_discarded_is_terminal`、`test_repeated_discard_is_idempotent` |
| 10 | 单主体、沿用撤销权限、跨租户 403 且无副作用 | 服务层 `VersionNotFound`（HTTP 统一映射 403，`_translate` 与撤销同一路径）；`test_cross_tenant_discard_is_rejected_without_side_effects` 断言审计/事件计数不变、状态仍是 DRAFT |

## 3. 契约扩展的兼容性说明（你要求终审核对）

- `event_envelope.json`：**只新增枚举值**，字段与 `schema_version="1.0"` 不变；现有消费者未见（仓库内无消费方）。
- `review_actions.from_status`：新列**可空**，历史行留 NULL，不回填、不猜；`DISCARD` 必写。
- `knowledge_versions.status`：新增取值；检索/证据端点的状态白名单是正向枚举，`DISCARDED` 天然不可见（已加断言）。
- OpenAPI：新增一个路径 + 一处动作说明，不改既有路径与响应结构。
- 迁移号：0013（0010 保持空号，符合「禁止倒序回填」；回滚 2 步会先撞 0013 的守卫）。

## 4. 实测

```
uv run pytest -q                      → 403 passed, 27.87s
uv run pytest -q -m requires_pg       → 254 passed, 149 deselected, 0 skip
uv run ruff check src tests migrations → All checks passed
uv run mypy                           → Success: no issues found in 51 source files
```

迁移与回滚（本机实测）：

```
0013: APPLIED
DISCARDED 版本 51；DISCARD 审计 51
<- 0013_down.sql
rollback 被拒绝: 回滚 0013 被拒绝，已整体中止（未做任何变更）：
  FKH_ROLLBACK_BLOCKED: 存在 51 个 DISCARDED 版本、51 条 DISCARD 审计，
  不得自动收紧约束或改写为 REVOKED；请先人工处置这些数据
回滚尝试后 0013: APPLIED；DISCARDED 版本仍在: 51
```

## 5. 未做（按你的裁决延后）

1. 物理清理与保留期限（ADR-05 未签）；误传客户资料的紧急清理期限待业务确认。
2. 后台 UI 入口（当前只有 HTTP 端点）。
3. 弃用的"重试投递同一事件标识"沿用 outbox 既有语义（relay 重试用同一行 id），未新增断言。

请裁决：**PASS** / **CHANGES_REQUESTED（逐条）**。
