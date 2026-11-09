# L1 送审：Phase 1 检索统计子系统（迁移 0037）

- 日期：2026-11-09
- 基准：`5d3e3be`（10 万 p50 2.71s / p95 3.63s）
- 目标：p50 ≤1s / p95 ≤2s；recall 不低于 v3
- 预算：本切片第 1 次 L1 调用。
- 沙箱：`read-only`；不要探索仓库，只基于本文判断。

## 一、实现

### 1) schema（0037）

- `retrieval_stats_state`：`tenant_id` PK；`source_revision`、`built_revision`、`generation`、`status('EMPTY','READY','FAILED')`、`refreshed_at`、`last_error`；
  `built_revision <= source_revision`；`READY` 必须有 built_revision。
- `retrieval_corpus_stats(tenant, generation, scope, internal, acl_policy_id, docs, total_len)`；
  `docs > 0`、`total_len >= 0`；PK 全键。
- `retrieval_term_stats` 同分组 + `term, df, docs`；PK 全键；`df >= 0`、`df <= docs`；复合 FK 到 corpus。
- 三表 `ENABLE/FORCE ROW LEVEL SECURITY` + 租户策略；term 有 `(tenant,generation,scope,internal,term)` 索引。

### 2) source revision

- `fkh_bump_retrieval_revision()` `SECURITY DEFINER`（固定 `search_path`）在
  `knowledge_chunks` / `knowledge_versions` / `acl_policies` 的 INSERT/UPDATE/DELETE 后递增；
- 同一事务同一 tenant 只递增一次（transaction-local GUC marker，避免 100k chunk 产生 100k 次更新）；
- 已有 tenant 在迁移时插入 `EMPTY` 状态；新 tenant 首次写入时创建。

### 3) 重建

`fkh.retrieval.stats.refresh_tenant(conn, tenant)`：
- 调用方 CLI 以 `SET TRANSACTION ISOLATION LEVEL REPEATABLE READ` 开启；
- 状态行 `FOR UPDATE` 按 tenant 串行化；
- 精确 base 统计（与 `VISIBILITY_PREDICATE` 同一谓词）+ 每 ACL `ts_stat` 精确 df；
- 写新 `generation`，状态 `built_revision=source_revision`、`READY/EMPTY` 原子发布；删除旧 generation；
- CLI：`python -m fkh.retrieval.stats --url ... --tenant ...` / `--all`。

### 4) 读路径

`_corpus_stats` 在 `version_ids is None` 时尝试 `load_snapshot`：
- 仅 `status READY/EMPTY` 且 `built_revision == source_revision` 时使用；
- 按请求主体对 corpus 的 ACL 分组执行 `evaluate`，累计 N/avg_len/df；ACL 拒绝组不计入；
- 统计缺失/失效/无该 scope/业务角色无新表权限 → 回退原 live `_STATS_SQL`；
- `version_ids` 非空（课程白名单）不走统计表，仍 live。
- **v3 候选/BM25 语义不变**；统计只是数据源替换。

## 二、证据

- `pytest -q` → **717 passed**；ruff/mypy 全绿。
- 新增 6 条 e2e：
  - stats 与 live 完全相等（同数据同主体）；
  - 同一刷新被不同主体复用，ACL deny 组不计入；
  - 刷新后写 chunk：`source_revision` 只 +1，`_corpus_stats` 回退 live 并看到新数据；
  - 二次刷新 generation+1、旧 generation 行删除、built=source；
  - 空语料 `EMPTY` 且 `n=0`（区别于未构建）；
  - 业务角色 RLS：无 tenant 0 行，只有本 tenant 行。
- 10 万性能（`retrieval_perf --refresh-stats`，同一 100k 合成数据）：
  - p50 **578.6ms**、p95 **1,481.8ms**；统计读 p50 36.8ms；candidate 40 窗口 p50 547ms；
  - 统计刷新 **23.9s**（管理/离线，非请求路径）；
  - 证据：`_evidence/retrieval_perf_stats_100k_20261109.json`。

## 三、请 L1 判断

1. `source_revision` 触发器（每事务每 tenant 一次）+ `built_revision` 相等才读，是否满足失效回退要求？
2. 读侧 `load_snapshot` 与候选不在同一显式 `REPEATABLE READ` 快照（API 事务为 READ COMMITTED）；
   但统计是已发布 generation 的显式快照、ACL/可见性仍实时过滤。是否可接受？若不接受，
   最小修复是请求级只读 REPEATABLE READ，还是只允许 CLI 预热 + 统计表只做离线？
3. `refresh_tenant` 在 CLI 的 REPEATABLE READ 中构建、状态行 `FOR UPDATE`、原子发布 generation，
   并发写被触发器阻塞后会递增 revision 使 built<source；该并发语义是否足够？
4. ACL 可加总性：stats 存主体无关 ACL 分组，读侧按主体 evaluate 后累计；同组内 ACL 判定
   是否必然一致？是否还需要把 policy revision 存入分组键？
5. `df <= docs` 用 term 表冗余 `docs` + CHECK 实现；是否接受，还是必须由 corpus FK 保证？
6. 旧实例业务角色没有新表 SELECT 权限时捕获 `InsufficientPrivilege` 回退 live；新实例
   `GRANT ... ON ALL TABLES` 覆盖。这个兼容策略是否可接受？
7. 目标已达标是否可声称？**未做并发/冷缓存**；刷新后 100k p50 579ms / p95 1.48s，
   但刷新前/失效期间回退 live 2.71s。请判断是否允许作为“已达标（需刷新）”的边界。

## 四、固定回复骨架

```text
VERDICT: PASS | CHANGES_REQUESTED

MUST_FIX:
- <问题>: <为什么阻塞> -> <最小修复>

SHOULD_FIX:
- <问题>: <建议>

BOUNDARY:
- <本轮不要求覆盖的项>
```
