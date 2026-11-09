# 检索统计子系统 Phase 1：10 万切片性能与一致性证据

- 日期：2026-11-09
- 提交：`f0b8281`（Phase 1）
- 迁移：`0037_retrieval_stats.sql`
- 代码：`src/fkh/retrieval/stats.py`、`src/fkh/retrieval/lexical.py`、`src/fkh/api/db.py`
- 状态：Phase 1 实现完成，待 L1 终审；默认 v3 未改。

## 1. 目标与口径

用户确认目标：10 万切片 **p50 ≤1s / p95 ≤2s**，recall 不低于 v3。

Phase 1 只替换 BM25 统计来源，不改候选/排序语义：

- `retrieval_stats_state` / `retrieval_corpus_stats` / `retrieval_term_stats`；
- 触发器维护 `source_revision`；`built_revision == source_revision` 且 READY 才使用；
- 失效/缺失/权限不足回退原 live `_STATS_SQL`；
- 请求级 `REPEATABLE READ`（search/context/answer/training ask），统计、ACL、候选同一快照；
- 刷新 CLI：`python -m fkh.retrieval.stats --url ... --tenant ...`（REPEATABLE READ，单 tenant 锁，generation 原子替换）。

## 2. 10 万性能（合成数据，单查询，非并发）

| 项 | 刷新统计表后 |
|---|---:|
| 总检索 p50 | **578.6 ms** |
| 总检索 p95 | **1,481.8 ms** |
| 统计读取 p50 | 36.8 ms |
| 候选（40 窗口）p50 | 547.4 ms |
| 统计刷新（离线） | 23.9 s |

证据：[`retrieval_perf_stats_100k_20261109.json`](./retrieval_perf_stats_100k_20261109.json)（由 `retrieval_perf --refresh-stats` 生成）。

对照基线（同工具、未刷新统计表）：p50 2,711ms / p95 3,632ms。
**限定条件**：刷新成功、`built=source`、之后没有使 source_revision 变化的提交；否则回退 live（约 2.7s）。
不宣称并发/生产 SLO。

## 3. 一致性证据

- `tests/e2e/test_retrieval_stats.py`（12 条）：
  - stats 与 live N/avg/df 逐项相等；
  - 同一刷新服务不同主体，ACL deny 组不计入；
  - 刷新后写 chunk：source_revision 只 +1，读路径回退 live；
  - 二次刷新 generation+1、旧 generation 删除；
  - 空语料 EMPTY 与未构建区分；
  - 业务角色 RLS、权限撤销后 savepoint 回退 live；
  - 非 REPEATABLE READ 拒绝刷新；
  - 双连接：写触发器被重建状态锁阻塞，提交后 source > built；
  - 40001 重试成功/三次失败外抛。
- `tests/e2e/test_retrieval_stats_recall.py`：
  - 复用 `_evidence/recall_cases_20261104.json` 的 **50 条冻结查询**；
  - 同一数据、同一主体，stats/live 的有序 top-10 `chunk_id + score` **全部相等**；
  - 统计路径实际命中（state READY, built=source）。

## 4. 运行与运维

```powershell
# 迁移
uv run python -m migrations.apply --url "$FKH_DATABASE_URL"

# 全量/单租户重建（离线；100k 约 24s）
uv run python -m fkh.retrieval.stats --url "$FKH_DATABASE_URL" --tenant <uuid>
uv run python -m fkh.retrieval.stats --url "$FKH_DATABASE_URL" --all
```

- 发布/撤销/版本/ACL 变更会通过触发器递增 `source_revision`；
- 未刷新期间读路径自动回退 live，不会返回过期可见性；
- 旧实例业务角色未授予新表 SELECT 时，savepoint 回退 live，不中断检索。

## 5. 边界

- 未做并发/冷缓存/生产规模压测；未改变 v3 候选与 BM25 语义。
- 统计刷新是显式运维动作（CLI）；未接自动调度/增量维护。
- 失效期间性能回到 live（100k 约 2.7s），不宣称整体请求路径始终达标。
- 统计表只影响排序统计；可见性与 ACL 仍由请求侧实时判定。
