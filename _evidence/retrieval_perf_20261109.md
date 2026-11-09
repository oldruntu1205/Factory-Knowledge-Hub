# 大租户词法检索性能基准（10 万切片，2026-11-09）

- 状态：**基准完成，未做优化**；不宣称达到任何 SLO（ADR-06 未签，目标值未定）。
- 数据：全部合成，无客户资料；评测库 `.perf-pgdata`（gitignore，可删）。
- 代码：`src/fkh/tools/retrieval_perf.py`
- 原始结果：`_evidence/retrieval_perf_20261109.json`

## 1. 口径

| 项 | 值 |
|---|---|
| 检索 | `LexicalRetriever.search`，`scoring=lexical-v3`，`max_results=10` |
| 数据 | 1k / 10k / 50k / 100k 个 child 切片，4 个 ACL 策略（均 allow roles=staff） |
| 查询 | 5 条合成描述类问题，每条预热 1 次后测 3 次 |
| 分解 | 总耗时、候选 SQL（`_CANDIDATE_SQL`）、BM25 全范围统计（`_corpus_stats`）分别计时 |
| 环境 | 本机 Windows + 内嵌 PostgreSQL；单连接串行；**非并发吞吐测试** |
| 命中 | 各规模平均 10 条，候选链路有效 |

> 查询含「规格/冷凝管/试管」等常见字，属于**偏宽的 OR 查询**；它测的是最坏一侧，
> 不是客户所有问句的平均值。

## 2. 结果

| 切片数 | 总 p50 | 总 p95 | 候选 SQL p50 | 统计 p50 | 统计 p95 |
|---:|---:|---:|---:|---:|---:|
| 1,000 | **24.9 ms** | 68.7 ms | 11.5 ms | 12.2 ms | 15.2 ms |
| 10,000 | **273.7 ms** | 358.7 ms | 142.0 ms | 130.8 ms | 158.4 ms |
| 50,000 | **1,797.0 ms** | 3,058.0 ms | 671.4 ms | 840.1 ms | 2,221.8 ms |
| **100,000** | **2,711.0 ms** | **3,632.1 ms** | **1,319.7 ms** | **1,430.1 ms** | **1,698.5 ms** |

读数：

1. **100k 块当前 p50 ≈ 2.7s、p95 ≈ 3.6s**，单次检索已经不适合交互式问答；相对 5k 块的 142ms 约 19 倍。
2. **统计查询约占一半**（100k：1.43s），**候选 SQL 也约占一半**（1.32s）。
   只做统计缓存/统计表，最多把 2.7s 降到 ~1.3s，仍然不可交互。
3. 统计成本随规模近似线性：1k 12ms → 10k 131ms → 50k 840ms → 100k 1430ms；
   与 `lexical_v3_benchmark_20261106.md` 的「统计是主成本」判断一致。
4. 候选 SQL 的成本来自 `ts_rank_cd` 对**所有 GIN 命中行**排序后取 200；
   查询越宽（常见字越多）越慢。
5. 总耗时比「候选 + 统计」略高（约 0–0.3s），是 200 条候选的 Python 分词、BM25 重排与 ACL 判定。

## 3. 结论

- **不能只做统计缓存**。需要同时处理：
  1. 统计：按租户/范围/ACL 预计算 `N / avg_len / df(term)`（统计表或物化视图 + 失效策略）；
  2. 候选：避免对全部 OR 命中做 `ts_rank_cd` 全排序，例如逐词 top-K 并集、两阶段候选，
     但必须重跑 recall@10 回归，不能静默降质。
- 在优化前，10 万块级只适合**离线/批处理**或**接受秒级等待的内部查询**；
  不能对客户承诺交互式延迟。
- 本轮**不修改**默认检索模式、不引入 schema、不改排序语义；先保留可复现基线。

## 4. 复现

```powershell
# 只跑 1k 冒烟
uv run python -m fkh.tools.retrieval_perf --pgdata .perf-pgdata --scales 1000 --iterations 1

# 完整 1k/10k/50k/100k
uv run python -m fkh.tools.retrieval_perf `
  --pgdata .perf-pgdata `
  --scales 1000 10000 50000 100000 `
  --iterations 3 `
  --output _evidence/retrieval_perf_20261109.json
```

> 同一进程内逐级插入并 `ANALYZE`；不同规模的租户/数据量一一对应。
> 结果受本机磁盘、PostgreSQL 缓存与并行度影响，不能直接外推到生产硬件。
