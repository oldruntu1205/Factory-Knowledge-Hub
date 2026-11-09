# L1 复送：放弃抽样，改为精确统计表 + 候选收窄

- 日期：2026-11-09
- 上一轮回执：`_evidence/l1_reply_retrieval_perf_20261109.md`（CHANGES_REQUESTED）
- 预算：本切片第 2 次 L1 调用。
- 事实基线：100k p50 2.71s；候选 1.32s + 统计 1.43s；拆出 base 统计 94ms；逐词 exact GIN 24–140ms。

## 一、根据 MUST_FIX 的调整

1. **不再用 `TABLESAMPLE` 抽样**。改为**精确统计表**，避免样本不足、块聚集、阈值波动。
2. **统计口径与候选完全同一条可见性谓词**（`VISIBILITY_PREDICATE`）；统计表只存
   `(tenant, scope, internal, acl_policy_id)` 维度的 `docs/total_len` 与 `term -> df`。
3. **统计刷新**：
   - 新增 `retrieval_corpus_stats` / `retrieval_term_stats`；
   - 刷新命令 `fkh.tools.retrieval_stats --tenant <uuid>` 做全量精确重建；
   - `ReviewService` 的 `approve_internal` / `publish` / `revoke` / `discard` 在同一事务内刷新
     （管理动作，允许秒级；批量导入用命令一次性刷新）。
   - 若统计表无该 `(tenant, scope, internal)` 行 → **回退现有 live `_STATS_SQL`**，
     不返回空/低质结果；回退原因记录在 `SearchOutcome` 诊断。
4. **候选收窄**：
   - 用统计表拿到全部 query term 的 exact df；
   - 丢弃 `df/N > 0.30` 的 term，至少保留 1 个；全部高频则回退全部 term（完整慢路径）；
   - 用保留 term 重构造 OR 候选；BM25 只对保留 term 计分；
   - 新 `scoring=lexical-v4`，默认**仍 v3**，直到 50 条冻结用例 recall ≥ `max(0.62, v3 实测)` 且
     10 万 p50 ≤1s / p95 ≤2s；v3 保留可回退。
5. **快照/一致性**：
   - 统计表是**显式快照**（`refreshed_at`），不是实时逐语句聚合；候选 SQL 仍在当前请求事务内用
     实时 visible/ACL 过滤，所以**权限与撤销即时生效**；统计表只影响排序/候选收窄，不决定可见性。
   - 统计刷新与业务状态变更同事务提交；批量导入后必须刷新，未刷新时回退 live。
6. **ACL 等价性**：
   - 统计刷新时按 ACL policy 分组，用与 `_STATS_SQL` 相同的 `evaluate` 规则过滤后再累计；
   - 新增测试：同一批数据下，统计表 `(N, avg_len, df)` 与 live 统计逐项相等（多主体、多 ACL、
     含拒绝策略/索引代次/撤销状态）；候选收窄后 top-10 与 v3 对比 recall。
7. **性能验收**：
   - 同一 `retrieval_perf` 工具，100k 数据先刷新统计，再测完整 `search`（含统计表读取、候选、
     BM25、ACL）；目标 p50 ≤1s、p95 ≤2s；记录缓存状态/选中 term/回退比例。

## 二、请 L1 判断

1. 用**精确统计表 + 发布/撤销时同步刷新**替代抽样，是否可解除上一轮抽样相关 MUST_FIX？
2. 统计表作为显式快照、可见性/ACL 仍由候选实时过滤，是否满足“同一快照”要求？若不满足，
   最小修复是请求级 `REPEATABLE READ` 还是统计表版本号 + 候选同版本过滤？
3. `ReviewService` 内同步刷新（100k 约 1–3s）是否可接受？还是必须异步刷新/脏标记 + 回退 live？
4. 丢弃 `df/N > 0.30` term 的 v4 语义变更，是否允许在 recall/性能双达标后启用？默认保持 v3
   是否足够？
5. 统计表 schema/RLS/索引是否有阻塞项？请给出最小字段/约束/刷新语义。

## 三、固定回复骨架

```text
VERDICT: PASS | CHANGES_REQUESTED

MUST_FIX:
- <问题>: <为什么阻塞> -> <最小修复>

SHOULD_FIX:
- <问题>: <建议>

BOUNDARY:
- <本轮不要求覆盖的项>
```
