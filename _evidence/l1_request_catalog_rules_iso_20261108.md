# L1 复审 2：目录规则 PUT 的隔离级别与等待后可见性（`2304686`）

- 日期：2026-11-08
- 上一轮回执：`_evidence/l1_reply_catalog_rules_lock_20261108.md`
  （条件 = 确认 PUT 使用 READ COMMITTED，且存量检查是取得排他锁后的独立 SQL）
- 请只做判断：**不要探索仓库、不要读源码、不要跑测试**。

## 1. 实现

`save_rule` 顺序：

1. `compile_rule(...)`（纯校验，不碰库）；
2. **`SHOW transaction_isolation`** → 不是 `read committed` 直接抛 `CatalogRuleConflict`
   （宁可拒绝，也不假装锁住就安全；REPEATABLE READ 下等待锁不刷新快照）；
3. 绑定租户 → `SELECT … FOR UPDATE`（拿到排他锁，若写入事务持共享锁则在此等待）；
4. **独立的**兼容性检查语句（两条 SELECT：货号两张表、货币集合）——READ COMMITTED 下
   每条语句取新快照，因此能看到等待期间提交的行；
5. 更新 + 写审计版本。

## 2. 验收：等待后可见性（新增，`test_put_sees_writes_committed_while_it_waited`）

真实 PG，两条连接：

1. 写入事务：`SELECT … FOR SHARE`（持规则共享锁）→ 插入一篇目录文档 + 版本 + 块 +
   一条**新规则下不合法**的货号条目（`A112420`，新规则是 `^SKU-\d{4}-\d{3}$`）；
2. 启动 PUT 线程（收紧规则），它会阻塞在 `FOR UPDATE`；
3. 写入事务 **commit**（共享锁释放）；
4. PUT 拿到排他锁后执行兼容性检查 → **必须**抛 `CatalogRuleConflict`（409 语义），
   用例断言不是"成功"。

若实现停留在旧快照（例如 REPEATABLE READ），这一步会误判兼容并成功——用例会红。

## 3. 其它已确认前提（对应你上一轮的第 3 条）

- 双方锁的是同一条租户规则行（`tenant_id` 为主键）；
- 锁、写入、检查在同一事务；
- 读路径（GET 配置）不加锁，不影响写入。

## 4. 门禁

```
uv run pytest -q            → 645 passed   exit=0
uv run ruff check src tests → All checks passed!  exit=0
uv run mypy                 → Success: no issues found  exit=0
```

## 5. 片外事项（按你的裁定不再扩大）

查询侧货号识别、历史重抽、回填使用版本追踪，均记在 `docs/SPEC/catalog-rules.md` §6。
