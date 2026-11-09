# L1 三送：Phase 1 统计子系统（补 40001 与 50 条 recall 证据）

- 日期：2026-11-09
- 上一轮回执：`_evidence/l1_reply_retrieval_stats_followup_20261109.md`
- 预算：本切片第 3 次 L1 调用（上限 3 次，本轮熔断点）。

## 一、补的 MUST_FIX

### 1) 40001 闭环（三类证据）

- **真实冲突**：`test_repeatable_read_conflict_is_serialization_failure`——
  两个 RR 事务先读同一 state 行，A 更新提交，B 再更新 → 断言 `SerializationFailure`。
- **重建恢复**：`test_refresh_recovers_from_real_snapshot_conflict`——
  A 持 state 行锁并修改 revision，B 的 `_refresh_one` 在锁上等待；A 提交后 B 完成重试，
  最终 `built_revision = source_revision`。
- **重试上限**：
  - `test_refresh_retries_serialization_failure`：前两次抛 40001，第三次成功，调用次数=3；
  - `test_refresh_gives_up_after_three_serialization_failures`：连续 3 次 40001 后向外抛。

### 2) 50 条冻结查询 stats/live 等价

`tests/e2e/test_retrieval_stats_recall.py`：
- 复用 `_evidence/recall_cases_20261104.json` 的 **50 条**；
- 同一数据/主体，stats 与 live 的**有序 top-10 `chunk_id + score` 全部相等**；
- 断言统计路径实际命中（`status=READY`、`built=source`）。

### 3) 修复了一个真实差异

对比 50 条时发现：`ts_stat.word` 对 Latin 做了小写规范化（`ACL → acl`），
原 `load_snapshot` 用原词匹配会把 `ACL/ADR/SQL...` 的 df 当成 0。
现已：
- 统计读取按 `lower(term)` 匹配；
- 返回时映射回查询原词，保持与 live `_STATS_SQL` 的 df key 一致；
- 50 条 top-10 因此完全相等。

## 二、证据

- `pytest -q` → **724 passed**（新增 15 条 stats 相关测试）；ruff/mypy 全绿。
- 10 万性能（统计有效）：p50 **578.6ms** / p95 **1481.8ms**；统计读 p50 36.8ms；
  刷新 23.9s（离线）。证据：`_evidence/retrieval_perf_stats_100k_20261109.json`。
- 性能表述限定：同一 100k 合成数据、刷新成功且无后续写提交；失效/未刷新回退 live 2.71s，
  不宣称生产 SLO。

## 三、请 L1 判断

1. 真实 40001 冲突 + 重建恢复 + 重试上限三类测试，是否解除上一轮 MUST_FIX？
2. 50 条冻结查询的 stats/live 有序 top-10 完全相等，是否足以作为 recall 未退化证据？
3. Phase 1 是否可 PASS？默认 v3 未改，失效期回退 live。

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
