# 迁移 0011 原子性与回滚顺序 · 实测证据（2026-11-04）

- 触发：L1 终审 CHANGES_REQUESTED 第 1、2 项（`_evidence/l1_reply_hybrid_wiring_final_20261104.md`）
- 方式：单进程内持有 pgserver 句柄，先在被隔离的 scratch 库里做失败注入与锁覆盖验证，
  再在真实测试库上做「两步回滚 → 重新应用」。脚本跑完即删（不留临时工具）。
- 环境：本机 pgserver 内嵌 PostgreSQL 16（pgvector），`uv run`，独占运行（无并发 pytest）。

## 1. 中途失败后的整体回滚（原子性）

做法：把 `migrations/0011_retrieval_mode.sql` 原文拼上一句 `SELECT 1/0;` 放进**同一个事务**执行，
模拟回填完成后、收尾阶段失败。

```
== 初始状态 ==
column_exists: False   trigger: O
== 注入失败：在 0011 之后追加除零，验证整段回滚 ==
失败注入生效: DivisionByZero
回滚后 column_exists: False
回滚后 trigger: O (O=启用, D=禁用)
回滚后数据: ['REFUSED', 'SUCCEEDED']   ← 原有 2 行未被审计模式污染
```

结论：迁移文件由 `apply.py` 的 `with conn.transaction():` 整段包裹，DDL（含
`ALTER TABLE ... DISABLE/ENABLE TRIGGER`）、`UPDATE` 回填、`SET NOT NULL`、`DROP DEFAULT`
在**同一事务**内；中途失败时列不存在、触发器保持启用、数据未被改写。

## 2. 禁用触发器期间的锁覆盖（其它会话写不进来）

做法：连接 A 执行 `ALTER TABLE ... DISABLE TRIGGER` 后**不提交**；连接 B 设置
`statement_timeout=800ms` 后尝试 INSERT。

```
== 锁覆盖：A 禁用触发器未提交时，B 的写入应被阻塞 ==
B 被 ACCESS EXCLUSIVE 锁住并超时（符合预期）
A 回滚后 B 写入成功；触发器状态: O
```

结论：`ALTER TABLE` 持有 ACCESS EXCLUSIVE，保护暂停区间内其它会话的读写都被挡住；
A 回滚后触发器立即恢复启用。

## 3. 回填正确性（有依据才回填）

```
== 正常应用 0011 ==
column_exists: True   trigger: O
回填结果: [('REFUSED', 'not_executed'), ('SUCCEEDED', 'lexical-v1')]
```

结论：召回前被拒的记录记为 `not_executed`（没有执行过任何算法），执行过词法的记 `lexical-v1`。

## 4. 两步回滚 → 重新应用（跳号不影响）

```
== 两步回滚（0011 + 0009）==
<- 0011_down.sql
<- 0009_down.sql
已回滚 2 个迁移。
0009    PENDING   0009_chunk_lexeme_tokenizer.sql
0011    PENDING   0011_retrieval_mode.sql
== 重新应用 ==
-> 0009_chunk_lexeme_tokenizer.sql
-> 0011_retrieval_mode.sql
已应用 2 个迁移。
0009    APPLIED   0009_chunk_lexeme_tokenizer.sql
0011    APPLIED   0011_retrieval_mode.sql
```

结论：回滚按**应用顺序**倒序取实际应用过的版本，跳过的 0010 不参与、不阻断。
同时 `apply.py` 新增「禁止历史回填」守卫：已应用最高版本之后才允许新增迁移，
预留给草稿终止的 0010 若晚落地必须改用新号（测试见 `tests/test_migration_tooling.py`）。
