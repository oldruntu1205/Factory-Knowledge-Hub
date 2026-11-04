# L1 复审：迁移跳号与 0011 原子性（`302c367`）

- 日期：2026-11-04
- 对应：`_evidence/l1_reply_hybrid_wiring_final_20261104.md`（CHANGES_REQUESTED 两项）
- 请只做判断：**不要探索仓库、不要读源码、不要跑测试**。

## 先更正一条事实

上一份终审材料里我写的「回滚 2 步会因 0010 缺 down 脚本显式失败」是**错的**：
旧实现 `sorted(applied, reverse=True)` 只取**已应用**的版本，0010 从未应用、根本不在集合里，
所以旧实现其实不会因此失败。该错误表述已从 HANDOFF §5 删除。仍按你的要求把
「回滚顺序」这一类的真实缺陷修掉，而不是只改文档。

## 1. 迁移跳号与回滚（第 1 项）

**实现改动**（`migrations/apply.py`）：

| 变更 | 说明 |
|---|---|
| `fetch_applied_rows()` | 按 `schema_migrations.applied_at, version` 返回应用顺序（原为按版本号） |
| `plan_rollback(rows, steps)` | 回滚目标 = 应用顺序的**倒序**取 steps；跳号不参与、不阻断 |
| `missing_rollback_scripts(targets)` | 回滚前整体校验 down 脚本齐备，缺一个即整体中止（不再"滚一半"） |
| `assert_no_backfill(files, applied)` | 禁止历史回填：`version <= max(applied)` 的待应用迁移直接拒绝，并提示改用新号 |
| `assert_no_drift` 保持不变 | 已应用迁移被修改仍然拒绝 |

**证据**：
- `tests/test_migration_tooling.py`（6 项，全绿）：应用顺序≠版本顺序时回滚取应用顺序
  （0011 先应用、0010 后应用 → 先回滚 0010）；跳号两步回滚；非正步数拒绝；
  缺 down 脚本被列出；倒序回填被拒；跳号但无回填放行。
- 真实测试库演练（`_evidence/migration_0011_atomicity_20261104.md` §4）：
  `--rollback 2` → 回滚 `0011_down`、`0009_down` → 状态 PENDING → 重新应用两个迁移 → 全 APPLIED。
- 0010 的处置已写入 HANDOFF §5 与 WORKING-AGREEMENT §2：预留号若已被跳过，
  该方案**必须改用新号**，不能补洞（由 `assert_no_backfill` 强制）。

## 2. 回填期间追加式保护的原子性（第 2 项）

**机制**：`apply.py` 用 `with conn.transaction():` 把「执行整个迁移文件 + 写 schema_migrations」
包在**同一事务**里。0011 的全部语句（`ADD COLUMN` → `DISABLE TRIGGER` → 两条 `UPDATE` 回填
→ `ENABLE TRIGGER` → `SET NOT NULL` → `DROP DEFAULT` → `ADD CONSTRAINT` → 自检 `DO` 块）
都在这一个事务内；PostgreSQL 的 `ALTER TABLE ... DISABLE TRIGGER` 是**事务性 DDL**，
失败时随事务回滚。

**实测**（完整输出见 `_evidence/migration_0011_atomicity_20261104.md`）：

1. **中途失败**：在 0011 原文后追加 `SELECT 1/0;`，同一事务执行 → 抛 `DivisionByZero` 后检查：
   列 `retrieval_mode` 不存在、触发器 `tgenabled = 'O'`（启用）、原有 2 行数据未被改写。
2. **锁覆盖**：会话 A 执行 `ALTER TABLE ... DISABLE TRIGGER` 后**不提交**，
   会话 B（`statement_timeout=800ms`）执行 INSERT → 被 ACCESS EXCLUSIVE 锁住并超时；
   A 回滚后 B 写入成功、触发器仍为 `O`。即保护暂停区间内其它会话无法读写该表。
3. **回填粒度**：`status='SUCCEEDED'` → `lexical-v1`；`status='REFUSED'` → `not_executed`。

结论：不需要为此改代码；上述为补证。若你认为还需要「迁移脚本级」的自动化回归
（而不是一次性演练 + 纯逻辑测试），请明示，我按你的口径补；否则本项按已闭环处理。

## 3. 本轮附带改动

- `tests/e2e/test_search_hybrid_api.py` 增 1 项：范围未授权（REFUSED）的审计模式必须是
  `not_executed`，不得伪记成执行过词法（Q3）。
- 文档同步：HANDOFF §5（跳号与回滚的真实行为）、WORKING-AGREEMENT §2（跳号/禁止回填/回滚顺序）。

## 4. 门禁（独家运行）

```
uv run pytest -q                      → 363 passed, 14.14s
uv run pytest -q -m requires_pg       → 230 passed, 133 deselected, 0 skip, 14.00s
uv run ruff check src tests migrations → All checks passed
uv run mypy                           → Success: no issues found in 50 source files
```

请裁决本次 CHANGES_REQUESTED 是否关闭：**PASS** / **仍 CHANGES_REQUESTED（仅列仍未满足的点）**。
若属「实现已满足、只是证据形式不同」，请直接判 PASS 并写明无需补充。
