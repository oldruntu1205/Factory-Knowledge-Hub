# 检索统计表（Phase 1）

- 日期：2026-11-09
- 提交：`f0b8281`
- 迁移：`0037_retrieval_stats.sql`
- 代码：`src/fkh/retrieval/stats.py`、`src/fkh/retrieval/lexical.py`

## 1. 为什么

`lexical-v3` 每次查询都要扫全范围做 BM25 的 `N / avg_len / df`；10 万切片时统计约 1.43s。
统计表把这份精确聚合预计算到 `(tenant, scope, internal, acl_policy_id)` 分组，读路径只做分组读取 +
请求侧 ACL 累计；不改候选/排序语义。

## 2. 数据模型

| 表 | 作用 |
|---|---|
| `retrieval_stats_state` | 每 tenant 的 `source_revision` / `built_revision` / `generation` / `status` |
| `retrieval_corpus_stats` | 分组 `docs` / `total_len` |
| `retrieval_term_stats` | 分组内 `term -> df`，复合 FK 保证 `term.docs = corpus.docs`，`df <= docs` |

三表均 `ENABLE/FORCE ROW LEVEL SECURITY` + 租户策略。

## 3. 失效与重建

- `knowledge_chunks` / `knowledge_versions` / `acl_policies` 的 INSERT/UPDATE/DELETE 触发器
  递增 `source_revision`；同一事务同一 tenant 只 +1。
- 只有 `status ∈ {READY, EMPTY}` 且 `built_revision = source_revision` 才使用统计表；
  否则回退 live `_STATS_SQL`。
- 重建必须在 **REPEATABLE READ** 事务中执行（函数会检查并拒绝其他隔离级别）：
  - 状态行 `FOR UPDATE` 按 tenant 串行化；
  - 新 `generation` 完整写入后原子发布 `built_revision`，再删除旧 generation；
  - 并发写被状态行锁阻塞；提交后其触发器递增 revision，使 `built < source`，读侧回退 live。
- CLI：

```powershell
uv run python -m fkh.retrieval.stats --url "$FKH_DATABASE_URL" --tenant <uuid>
uv run python -m fkh.retrieval.stats --url "$FKH_DATABASE_URL" --all
uv run python -m fkh.retrieval.stats --url "$FKH_DATABASE_URL" --dirty
uv run python -m fkh.retrieval.stats --url "$FKH_DATABASE_URL" --watch --interval 5 \
  --alert-webhook "https://alerts.example/hooks/fkh"
# 或环境变量 FKH_STATS_ALERT_WEBHOOK
```

- `--dirty`：只处理未构建、`built_revision != source_revision`、或 `status=FAILED` 的 tenant；
- `--watch`：持续巡检（发布/撤销后异步重建），单 tenant 失败会写 `status=FAILED/last_error`、
  stderr 输出 `retrieval_stats_refresh_failed` 事件并返回非 0；查询侧继续回退 live；
- `--alert-webhook` / `FKH_STATS_ALERT_WEBHOOK`：失败轮 POST `retrieval_stats_alert`
  （含 dirty/refreshed/failed 与 tenant/error）；投递失败输出
  `retrieval_stats_alert_delivery_failed`，不改变刷新退出码；
- `mark_failed` 是条件更新：若已有 `built=source` 且 `READY/EMPTY` 的新成功，旧失败只上报、
  不覆盖新状态；
- 实例工具入口：

```powershell
uv run python -m fkh.tools.instance refresh-stats --workdir .instance-customer-x
uv run python -m fkh.tools.instance status --workdir .instance-customer-x
```

`status` 会输出 `retrieval_stats`（status/source_revision/built_revision/generation/refreshed_at/last_error）。
Docker Compose 已提供 `stats-refresher` 服务（owner 连接，`--watch --interval 5`）。

## 4. 读路径

- `/v1/knowledge/search`、`/v1/knowledge/context`、`/v1/knowledge/answer`、`/v1/training/ask`
  使用请求级 `REPEATABLE READ`（`get_read_conn` / `tenant_connection(isolation=...)`），
  统计、ACL、候选同一快照；
- 统计表按 ACL 分组返回，请求侧对每组执行 `evaluate`，只累计允许组；
- 课程问答带 `version_ids` 白名单时不使用统计表（仍 live），避免课程外统计混入；
- 旧实例业务角色无统计表 SELECT 时，savepoint 回退 live。

## 5. 性能与边界

- 10 万合成数据、统计有效：p50 **579ms** / p95 **1.48s**；统计读 p50 36.8ms；刷新约 **24s**（离线）。
- 未刷新/失效期间回退 live（约 2.71s）；不宣称请求路径始终达标。
- 未做并发/冷缓存/增量刷新；未改变 v3 候选与 BM25 语义。
