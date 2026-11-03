# L1 送审稿：outbox 事件投递器（relay）的 schema 与并发语义

- 日期：2026-11-03
- 提出方：L2 实施
- 需裁决方：L1 架构
- 风险归类命中：**数据库 schema 与迁移**、**并发/锁/事务**
- 请只做判断：**不要探索仓库、不要读其他文件、不要跑测试**。下面的事实已由 L2 取证，行号可用于核对。

## 1. 背景（一句话）

L1 §7.2 要求「业务状态与 outbox 写入同一 PostgreSQL 事务；至少一次投递；消费者按 `(tenant_id,event_id)` 幂等去重；支持死信与重试」。
当前 `ReviewService` 已在发布/撤销时写 `outbox_events`，但**没有任何投递器**，事件只会停在表里。本次要补 relay。

## 2. 事实（已取证，不要自行探索）

**表结构**（`migrations/0001_core_schema.sql`）：

```sql
CREATE TABLE outbox_events (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id         uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
  aggregate_type    text NOT NULL,
  aggregate_id      uuid NOT NULL,
  aggregate_version integer NOT NULL,
  event_type        text NOT NULL,
  payload           jsonb NOT NULL,
  occurred_at       timestamptz NOT NULL DEFAULT now(),
  published_at      timestamptz,
  retry_count       integer NOT NULL DEFAULT 0 CHECK (retry_count >= 0),
  UNIQUE (tenant_id, aggregate_type, aggregate_id, aggregate_version, event_type)
);
CREATE INDEX idx_outbox_pending ON outbox_events (occurred_at) WHERE published_at IS NULL;
```

**RLS**（`migrations/0004_rls.sql`）：`outbox_events` 已 `ENABLE + FORCE ROW LEVEL SECURITY`，策略
`USING (tenant_id = fkh_current_tenant()) WITH CHECK (...)`；`fkh_current_tenant()` 读 `app.tenant_id`，
未设置时返回 NULL ⇒ 对业务角色**零行可见**。仅超级用户或 `BYPASSRLS` 角色可跨租户。

**写入点**（`src/fkh/domain/review.py` 的 `_emit`）：只写 `tenant_id/aggregate_type/aggregate_id/aggregate_version/event_type/payload`，
**没有 trace_id、没有 occurred_at 的显式值**（走默认 now()）。

**事件封套契约**（`contracts/schema/event_envelope.json`，L1 §7.1）：
`additionalProperties:false`，required = `schema_version, event_id, event_type, tenant_id, aggregate_id,
aggregate_version, occurred_at, trace_id, producer, payload`；`trace_id` 为 `string, minLength:1`。

**迁移执行器**（`migrations/apply.py`）：`sorted(glob("[0-9][0-9][0-9][0-9]_*.sql"))` 顺序应用，
每个迁移必须有同名 `rollback/NNNN_down.sql`，且**已应用迁移内容被修改即判定漂移并拒绝执行**。

**测试装置**（`tests/conftest.py`）：会话级共享一个真实 PostgreSQL 16（pgserver/pgvector），
业务断言连接 `SET ROLE fkh_app`（NOBYPASSRLS），造数用超级用户连接。

## 3. 拟议 DDL（待判）

新增 `migrations/0006_outbox_delivery.sql`：

```sql
ALTER TABLE outbox_events ADD COLUMN trace_id        text;
ALTER TABLE outbox_events ADD COLUMN next_attempt_at timestamptz NOT NULL DEFAULT now();
ALTER TABLE outbox_events ADD COLUMN locked_until    timestamptz;
ALTER TABLE outbox_events ADD COLUMN last_error      text;
ALTER TABLE outbox_events ADD COLUMN dead_lettered_at timestamptz;

COMMENT ON COLUMN outbox_events.trace_id IS
  '产生该事件的事务链路 ID；历史行与未传 trace 的写入点为 NULL';

DROP INDEX idx_outbox_pending;
CREATE INDEX idx_outbox_pending ON outbox_events (next_attempt_at, occurred_at)
  WHERE published_at IS NULL AND dead_lettered_at IS NULL;
CREATE INDEX idx_outbox_aggregate_order ON outbox_events (tenant_id, aggregate_type, aggregate_id, occurred_at)
  WHERE published_at IS NULL;
```

回滚脚本删除新增列并还原旧索引。**为回避编号撞车：0006 归本切片；若后续「DRAFT 终止路径」获准，使用 0007。**

## 4. 拟议领取 / 确认 / 失败语义（待判）

```sql
-- 领取（单事务，返回本批租约行）
WITH due AS (
  SELECT o.id
    FROM outbox_events o
   WHERE o.published_at IS NULL
     AND o.dead_lettered_at IS NULL
     AND o.next_attempt_at <= now()
     AND (o.locked_until IS NULL OR o.locked_until < now())
     -- 同一聚合串行：只要还有更早的未发布事件，本次不领后续事件
     AND NOT EXISTS (
       SELECT 1 FROM outbox_events p
        WHERE p.published_at IS NULL AND p.dead_lettered_at IS NULL
          AND p.tenant_id = o.tenant_id AND p.aggregate_type = o.aggregate_type
          AND p.aggregate_id = o.aggregate_id
          AND (p.occurred_at, p.id) < (o.occurred_at, o.id))
   ORDER BY o.occurred_at
   LIMIT %s
     FOR UPDATE SKIP LOCKED
)
UPDATE outbox_events t
   SET locked_until = now() + %s::interval
  FROM due
 WHERE t.id = due.id
RETURNING t.*;
```

- **确认**：`published_at = now(), locked_until = NULL, last_error = NULL`
- **失败**：`retry_count = retry_count + 1, locked_until = NULL, last_error = ...(截断 500 字符), next_attempt_at = now() + backoff`
- **死信**：`retry_count >= max_attempts` 时置 `dead_lettered_at = now()`，需人工重放（重放即清空该列与 retry_count）
- 退避：`min(base * 2^retry_count, cap)`，默认 base=2s、cap=5min、max_attempts=8，全部走配置

## 5. 逐条待判问题

1. **relay 的数据库身份**：跨租户扫描与 FORCE RLS 冲突。建议新建平台角色 `fkh_relay`（`BYPASSRLS`，仅授予
   `SELECT/UPDATE ON outbox_events`），业务角色保持零可见。是否可接受？还是要求 relay 逐租户 `set_config` 扫描
   （每轮 N 次事务，租户增长后退化为 O(N)）？后者更保守但更慢，请裁决取哪个。
2. **租约列是否必要**：若只靠 `published_at IS NULL` + `FOR UPDATE SKIP LOCKED`，锁在长事务内持有到投递完成；
   若投递是 HTTP 调用，等于「数据库行锁 + 网络 IO」，worker 崩溃即释放但会造成重复领取抖动。
   加了 `locked_until` 后崩溃恢复依赖租约到期（默认 30s）。请裁决：接受租约，还是接受「锁持有到投递完成」。
3. **`trace_id` 缺失的降级**：封套契约要求 `trace_id` 非空，但现有写入点不传。
   建议：relay 在 `trace_id IS NULL` 时用 `event_id` 填充，并在 `docs/SPEC/outbox-delivery.md` 注明
   「该字段此时表达事件自身链路，不代表上游请求 trace」，同时登记后续由写入点补真实 trace。
   是否接受该降级？若不接受，则必须先改 `review-service` 写入点（跨切片协调），本切片被阻塞。
4. **同聚合串行**：上面 `NOT EXISTS` 的排序键取 `(occurred_at, id)`。风险是同一事务内多条事件的
   `occurred_at` 完全相同（默认 now() 同事务恒定），此时靠 `id`（uuid 随机）排序**不等于**业务插入顺序。
   是否应改为按 `id` 单调键（例如改用 `bigserial` 或 `xmin`）？若维持现状，请明确「同事务多事件不保证顺序」
   是否可接受（发布/撤销在同一事务发生的概率）。
5. **投递失败上限与死信语义**：`max_attempts=8`、指数退避、死信后不自动复活是否合适？
   死信是否需要 `outbox_events` 上的「待人工重放」可见性（例如运维视图）？
6. **传输层未配置时的行为**：建议 fail-closed——relay 启动时若无可用 transport 则直接拒绝运行，
   绝不把事件标记为已发布（否则等于静默丢事件）。是否需要额外的「本地落盘队列」兜底？建议不要，理由：
   落盘队列会把一致性责任从数据库搬到一个没人监控的文件上。
7. **重放与幂等**：人工重放是否允许把 `published_at` 清空（重新投递）？消费者侧幂等键
   `(tenant_id, event_id)` 已由 §7.2 规定，relay 在 HTTP 传输时应带 `Idempotency-Key: <event_id>`。请确认。

## 6. 固定回复骨架（请按此结构回）

```
【判决】1..7 逐条：采纳 / 改法（给出替换 SQL 或语义）
【必须改的 DDL】完整列定义（可直接粘进迁移）
【必须补的测试】按"能失败的断言"描述，3~8 条
【可延后】明确列为后续切片的项
【风险提示】你认为最容易在上线后咬人的一条
```

## 7. 边界与预算

- 只做判断与给 SQL/断言，不需要写实现代码、不需要跑测试、不要探索仓库。
- 本切片预期 L1 调用 1 次（若判决要求改设计，最多再 1 次）。
- 采纳情况与花费会在 `docs/SPEC/outbox-delivery.md` 的「裁决落实」一节登记。
