# L1 送审：检索统计刷新自动化/运维接线（Phase 1.1）

- 日期：2026-11-09
- 基准：`f0b8281` + `5570c21`（统计表 Phase 1 已 PASS）
- 沙箱：`read-only`；不要探索仓库，只基于本文判断。
- 预算：本切片第 1 次 L1 调用。

## 一、目标

把“发布/撤销后统计失效、需手动 CLI”接成可运维的异步刷新：
**失效回退 live 的安全性不变；刷新失败可见、可告警、可重试。**

## 二、实现

### 1) `fkh.retrieval.stats` 新增

```text
--dirty   只处理 built_revision IS NULL / built != source / status='FAILED'
--watch   持续巡检（默认 5s；失败轮退出非 0，交给进程守护重启）
--interval / --max-iterations
```

- `dirty_tenants(conn)`：按 state 查询；
- `refresh_dirty_once(url)`：逐 tenant `_refresh_one`，单个失败不中断其他 tenant；
- 失败时 `mark_failed(url, tenant, error)` 在**独立事务**写
  `status='FAILED'`、`last_error`（截断 1000 字）；stderr 输出结构化 JSON 事件；
- 成功时现有 `refresh_tenant` 写 `READY/EMPTY`、`last_error=NULL`；
- `--watch` 任一轮有失败即返回 1（进程守护重启/告警），不做无限吞错。

### 2) 实例工具

```text
instance refresh-stats --workdir ...   # 用管理员口令，REPEATABLE READ，重建
instance status ...                    # 额外输出 retrieval_stats 状态
```

`status` 对未应用 0037 的旧实例输出 `unavailable（先应用迁移 0037）`。

### 3) 编排

`deploy/compose/docker-compose.yml` 新增 `stats-refresher` 服务：
owner 连接、`--watch --interval 5`、`restart: unless-stopped`、依赖 db healthy。
API 仍用业务角色；统计表刷新权限不暴露给 API。

## 三、证据

- 新增 2 条 e2e：
  - dirty + watch 单轮：写入后 dirty，`refresh_dirty_once` 刷新到 READY；`_watch(max_iterations=1)` 返回 0；
  - 刷新失败：`mark_failed` 写 FAILED/last_error，`_corpus_stats` 仍回退 live 并看到新增块。
- `pytest -q` → **728 passed**；ruff/mypy 全绿。
- 上一轮统计性能/一致性证据不变：p50 578.6ms / p95 1481.8ms（统计有效时）。

## 四、请 L1 判断

1. 轮询 `--watch` + 脏状态 + 失败标记 + 非 0 退出，是否满足“发布后异步刷新、失败告警”？
   还是必须改用 LISTEN/NOTIFY、任务队列或进程内后台线程？
2. 失败时用独立事务写 `FAILED/last_error`；读路径继续回退 live；单 tenant 失败不阻断其他
   tenant。该语义是否足够？
3. 刷新进程使用 **迁移/owner 连接**（Docker Compose 中 `POSTGRES_OWNER` 为超级用户；
   实例工具用 `postgres`），是否可接受？还是必须建 `BYPASSRLS` 维护角色/改用
   SECURITY DEFINER 函数？
4. `--watch` 失败后退出 1、由进程守护重启，是否比“循环内继续重试”更合适？
5. 本轮无 schema 变更；是否可 PASS？

## 五、固定回复骨架

```text
VERDICT: PASS | CHANGES_REQUESTED

MUST_FIX:
- <问题>: <为什么阻塞> -> <最小修复>

SHOULD_FIX:
- <问题>: <建议>

BOUNDARY:
- <本轮不要求覆盖的项>
```
