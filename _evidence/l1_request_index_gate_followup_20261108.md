# L1 复核：索引闸门两处阻断项已补齐（`d74e983`）

- 日期：2026-11-08
- 仅复核你在 `_evidence/l1_reply_index_gate_20261108.md` 提出的第 3、4 条。
- 请只做判断：**不要探索仓库、不要读源码、不要跑测试**。

## 阻断项 3：撤销清理必须有持久消费闭环

**迁移 `0025`**：`outbox_deliveries(tenant_id, event_id, consumer, status, attempts, last_error, updated_at)`，
主键 `(tenant_id, event_id, consumer)`，外键指向 `outbox_events(tenant_id, id)`，RLS 与其它表同构。

**消费者 `fkh.workers.revoke_purge`**：

```python
pending = SELECT e.id ... FROM outbox_events e
           LEFT JOIN outbox_deliveries d ON (同租户+同消费者)
          WHERE e.event_type IN ('...revoked.v1','...discarded.v1')
            AND (d.status IS NULL OR (d.status='PENDING' AND d.attempts < 5))
for each event:
    try:  purge_version_chunks(conn, tenant, version)   # 幂等删除
          标记 DONE（attempts+1）; commit
    except:
          rollback; 标记 PENDING/FAILED（attempts+1, last_error）; commit
```

- 幂等：`DONE` 不再进入 pending；
- 重试：`PENDING && attempts<5` 才会被再次取出；
- 死信：耗尽转 `FAILED` 并写 `last_error`；`failed_deliveries()` 列出，`reset_failed()` 允许补偿重入；
- 通用框架（多消费者/调度/指标）仍留 ADR-06。

**故障注入验收**（`tests/e2e/test_revoke_purge_consumer.py`，真实 PG）：

1. 撤销 → 消费一次 → 块计数 0、台账 `DONE/attempts=1`；再消费一次 → `processed=0`（幂等）；
2. 注入恒失败的 `purge` → 连续 5 轮后 `failed_deliveries()` 有 1 条、`attempts>=5`、`last_error` 含注入错误；
   期间块**未被误删**；
3. `reset_failed()` 后再次消费 → 完成、块计数 0。

## 阻断项 4：公开证据直读的水位闸门

`_CHUNK_SQL` 与 `_PARENT_SQL` 增加（用 `c.tenant_id` 关联，保持原位置参数风格）：

```sql
AND (
  v.status <> 'PUBLISHED_PUBLIC'
  OR c.index_generation <= COALESCE(
       (SELECT generation FROM index_visibility iv WHERE iv.tenant_id = c.tenant_id), 0)
)
```

验收（`tests/e2e/test_index_gate.py::test_public_evidence_direct_read_respects_watermark`）：
公开版本发布后直读 200；**删除水位行**后同一块直读 403——与"检索为空"口径一致。
内部内容（`APPROVED_INTERNAL`）仍按状态 + 范围判定，未加水位。

## 需要你判断的三点

1. 消费端的"上限 5 次"是我选的默认值；是否需要可配置或与 ADR-06 的全局重试策略对齐后统一？
2. 我在测试辅助 fixture 里为"用 SQL 直接造 PUBLISHED_PUBLIC"的旧用例补了等价的水位行
   （因为真实发布动作会切水位）。这不是放松断言，而是让 fixture 与发布语义一致——
   若你认为旧用例应当改为走真实发布接口，我下一片统一改。
3. 证据直读闸门只对公开内容启用（内部不加）。这是否与你要求的"公开可见性语义一致"吻合？

## 门禁

`uv run pytest -q` → **601 passed**；`ruff` / `mypy` 全绿。
