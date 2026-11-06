# L1 复审（第 2 轮）：非空条目基线的回滚证据（`aabb69a`）

- 日期：2026-11-06
- 对应：`_evidence/l1_reply_catalog_conflicts_rereview_20261106.md`（仍 CHANGES_REQUESTED 1 项）
- 请只做判断：**不要探索仓库、不要读源码、不要跑测试**。

## 1. 你的意见成立

原回滚用例的基线是 `catalog_items = 0`：冲突处理里"删除旧条目"那一步没有被真正覆盖，
回滚后"条目数不变"可能只是因为它本来就是 0。

## 2. 新增用例（非空基线）

`tests/e2e/test_catalog_conflicts.py::test_failure_after_removing_existing_item_restores_it`：

1. 入库单行目录 `A112420 200mm 39.00` → 正常生成 **1 条** `catalog_items`；
2. 审核通过（`APPROVED_INTERNAL`）；
3. 记录基线行（`chunk_id / raw_text / spec / raw_price / price / currency` 全部字段）；
4. 追加一个**冲突块**（同货号不同原文行），使下一次抽取走"建组 → 删旧条目 → 写候选"路径；
5. `monkeypatch` 把 `add_candidates` 换成"先真的写入候选、再抛 `RuntimeError`"；
6. 在保存点事务内执行抽取，断言异常抛出且 `calls >= 1`（确实走到了候选写入）；
7. 回滚后断言：
   - **原条目仍在**，且 6 个字段与基线逐一相同（不是"数量相同"就算过）；
   - `catalog_conflict_groups` 为 0；
   - `catalog_conflict_candidates` 为 0。

也就是说：这次覆盖了"先删旧条目、再写候选、中途失败"的真实顺序，回滚后旧值完整复现。

## 3. 门禁（独家运行）

```
uv run pytest -q                      → 462 passed, 50.15s
uv run pytest -q -m requires_pg       → 291 passed, 171 deselected, 0 skip
uv run ruff check src tests migrations → All checks passed
uv run mypy                           → Success: no issues found in 62 source files
```

冲突队列用例共 **14 项**（原 11 + DROP_ALL 重抽取 1 + 空基线回滚 1 + 非空基线回滚 1）。

请裁决：**PASS** / **仍 CHANGES_REQUESTED（仅列未满足点）**。
