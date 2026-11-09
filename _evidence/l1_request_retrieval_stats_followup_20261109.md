# L1 复送：Phase 1 统计子系统（回应 CHANGES_REQUESTED）

- 日期：2026-11-09
- 上一轮回执：`_evidence/l1_reply_retrieval_stats_phase1_20261109.md`
- 预算：本切片第 2 次 L1 调用。

## 一、MUST_FIX 修复

### 1) 请求级同一快照

新增 `get_read_conn`：在 `bind_tenant` **之前**执行
`SET TRANSACTION ISOLATION LEVEL REPEATABLE READ`，用于：
- `/v1/knowledge/search`
- `/v1/knowledge/context`
- `/v1/knowledge/answer`（`tenant_connection(... isolation="REPEATABLE READ")`）
- `/v1/training/ask`

统计读取、ACL 判定、候选/BW25 都在同一连接同一事务内；审计写入也在该事务内，
因此用 read-write REPEATABLE READ，而非 READ ONLY。写端点仍用默认隔离级别。

### 2) 权限异常回退闭环

`_corpus_stats` 用 **savepoint** 包住 `load_snapshot`：

```python
try:
    with self._conn.transaction():
        snapshot = load_snapshot(...)
except psycopg.errors.InsufficientPrivilege:
    snapshot = None   # savepoint 已回滚，事务可继续走 live
```

新增测试：撤销业务角色对三张统计表的 SELECT → `_corpus_stats` 仍返回 live 结果（n=1），
不抛 aborted transaction。

### 3) 并发重建 40001 重试 + 双连接测试

- CLI `_refresh_one` 对 `psycopg.errors.SerializationFailure` 重试整个 REPEATABLE READ 事务（最多 3 次）。
- 新增双连接测试：A 锁状态行模拟重建，B 写入 chunk 的触发器被阻塞；A 提交后 B 完成，
  `source_revision > built_revision`，不会错误保持 READY。
- `refresh_tenant` 现在检查 `SHOW transaction_isolation`，非 REPEATABLE READ 直接拒绝；
  新增拒绝测试。

### 4) recall 证据

新增同数据、同主体、同查询集（3 条）的 **stats vs live top-10** 对比：
chunk_id + score 完全相等。候选/排序算法未改，统计值逐项相等，因此 top-10 等价。
（未重跑 50 条冻结集；如 L1 要求，可再跑。）

## 二、SHOULD_FIX 处理

- **revision 契约**：每事务每 tenant 一次；覆盖 chunks/versions/acl；同一事务禁止
  bump→refresh→继续写（CLI 独立事务，不做此模式）。
- **ACL 加总**：统计按 ACL policy 分组、每 chunk 只属于一个组；读侧按主体 `evaluate` 后累计；
  policy 修改触发 source_revision 失效。新增 deny 组测试。
- **`df <= docs`**：corpus 增加包含 `docs` 的 UNIQUE，term 用包含 `docs` 的复合 FK，
  数据库强制 term.docs = corpus.docs。
- **性能表述**：只声明“同一 100k 合成数据、刷新成功且统计有效（built=source、无后续写提交）
  时，本次 p50 578.6ms / p95 1481.8ms”；刷新前/失效后回退 live 2.71s，不宣称整体生产 SLO。

## 三、证据

- `pytest -q` → **721 passed**；ruff/mypy 全绿。
- 10 条 stats e2e（等价、ACL、失效、generation、空语料、RLS、权限回退、隔离级别拒绝、锁等待、top-10 等价）。
- 10 万性能：p50 578.6ms / p95 1481.8ms；统计读 p50 36.8ms；刷新 23.9s（离线）。
  证据：`_evidence/retrieval_perf_stats_100k_20261109.json`。

## 四、请 L1 判断

1. 请求级 REPEATABLE READ + savepoint + 40001 重试 + 双连接锁测试，是否解除 ②③⑥？
2. stats vs live top-10 等价（3 条查询）是否足以作为 recall 未退化证据？若不够，最小补测是什么？
3. 性能边界按上述限定后，是否可 PASS Phase 1？默认 v3 未改，失效期回退 live。

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
