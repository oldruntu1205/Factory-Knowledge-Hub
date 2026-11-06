# L1 复审：冲突队列的两项补证（`c59461d`）

- 日期：2026-11-06
- 对应：`_evidence/l1_reply_catalog_conflicts_final_20261106.md`（CHANGES_REQUESTED 2 项）
- 请只做判断：**不要探索仓库、不要读源码、不要跑测试**。

## 1. 第 1 项：DROP_ALL 后重抽取的端到端断言

新增 `tests/e2e/test_catalog_conflicts.py::test_reextract_after_drop_all_keeps_decision`，步骤：

1. 造冲突（入库原文 + 两条冲突行）→ 组 OPEN；
2. `resolve_group(... drop_all=True)` → 组 `DROP_ALL`，条目不存在；
3. **对该版本重新抽取**（`IngestionRepository.extract_catalog_items`）；
4. 断言：
   - 组状态仍为 `DROP_ALL`（重抽取不得改写已生效决议）；
   - `catalog_items` 中该货号仍为 0 条；
   - 问答该货号返回 `REFUSED / NO_EVIDENCE`，且 `citations == []`。

## 2. 第 2 项：抽取中途失败的原子回滚

新增 `test_extraction_failure_rolls_back_queue_and_items`：

1. 基线：1 个待办组、3 条候选、0 条 `catalog_items`；
2. 用 `monkeypatch` 把 `fkh.workers.repository.add_candidates` 换成"先真的写入一部分候选、再抛 `RuntimeError`"；
3. 在 `app_conn.transaction()`（保存点）内执行 `extract_catalog_items`，断言异常抛出且 `calls >= 1`（确实走到了候选写入）；
4. 回滚后断言**三项都回到基线**：
   - `catalog_conflict_groups` 数量不变；
   - `catalog_conflict_candidates` 数量不变；
   - `catalog_items` 数量不变（原有条目没有被"移除旧条目"那一步弄丢）。

## 3. 门禁（独家运行）

```
uv run pytest -q                      → 461 passed, 56.29s
uv run pytest -q -m requires_pg       → 290 passed, 171 deselected, 0 skip
uv run ruff check src tests migrations → All checks passed
uv run mypy                           → Success: no issues found in 62 source files
```

冲突队列用例共 13 项（原 11 + 本次 2）。

请裁决：**PASS** / **仍 CHANGES_REQUESTED（仅列未满足点）**。
