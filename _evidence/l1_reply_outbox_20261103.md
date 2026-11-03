【判决】

**1. 数据库身份：采纳专用 `fkh_relay`，不采用逐租户扫描。**

允许平台专用角色具有 `BYPASSRLS`，权限收紧为：

- `SELECT ON outbox_events`；
- 仅对投递状态列授予列级 `UPDATE`；
- 不授予修改租户、事件身份、顺序、封套内容的权限；
- 不向业务角色授予该角色或其凭据。

角色由平台管理身份创建。`BYPASSRLS` 是角色属性，不能通过修改 RLS 策略模拟。业务角色继续受现有 RLS 约束；未设置租户时零行可见。

**2. 租约：采纳，但必须增加领取令牌，不能仅有 `locked_until`。**

增加 `delivery_token uuid`，每次领取生成新值。领取事务提交后才进行 HTTP 投递。

确认、失败、续租均须使用以下条件更新：

```sql
WHERE id = :event_id
  AND delivery_token = :delivery_token
  AND locked_until > clock_timestamp()
  AND published_at IS NULL
  AND dead_lettered_at IS NULL
```

更新零行表示租约已失效，该 worker 必须停止修改此事件。成功或失败时同时清空 `locked_until`、`delivery_token`。

**修改尝试次数定义：`retry_count` 表示已领取的投递尝试次数，在领取时加一，失败时不再加。** 这样 worker 持续崩溃也会耗尽预算。领取须满足 `retry_count < max_attempts`；最后一次领取后崩溃，由过期清理流程将达到上限且租约已过期的行转为死信，并清空租约。

30 秒可作为默认值，但必须覆盖等待发送及请求耗时；领取量不得超过可及时处理的容量，必要时按令牌续租。令牌只能阻止旧 worker 更新数据库，不能撤回已经发出的 HTTP 请求，重复投递仍由消费者去重。

**3. `trace_id`：采纳降级。**

封套使用：

```sql
COALESCE(trace_id, id::text)
```

历史行和当前写入点可继续写 NULL；非 NULL 值必须有非空白内容。文档按提案说明其含义。同一事件的重试、重放必须保持封套身份及内容稳定，不能临时生成新 trace。

封套仍须严格满足现有 schema；租约、重试等数据库字段不得直接透传。

**4. 同聚合串行：修改为业务顺序，不接受依赖发生概率放宽。**

采用 `(aggregate_version, event_ordinal)`；新增 `event_ordinal`，默认 `0`。同版本只有一条事件时无需修改现有写入点；同版本多事件必须由生产者在原业务事务内显式填写不同 ordinal。

不采用随机 UUID、`bigserial` 或 `xmin` 推断业务顺序：它们不能保证业务版本或提交顺序。

领取查询的前驱判断替换为：

```sql
AND NOT EXISTS (
  SELECT 1
    FROM outbox_events p
   WHERE p.published_at IS NULL
     AND p.tenant_id = o.tenant_id
     AND p.aggregate_type = o.aggregate_type
     AND p.aggregate_id = o.aggregate_id
     AND (p.aggregate_version, p.event_ordinal)
         < (o.aggregate_version, o.event_ordinal)
)
```

**前驱包含死信、退避中及已领取事件。死信阻塞本聚合后续事件。** 不同聚合继续推进。

生产者还必须保证：聚合版本更新经过并发控制，同版本事件原子写入，不在后续事务补写较小版本或 ordinal。relay 无法阻止数据库快照中尚不可见的前驱被晚提交。

若历史数据存在同版本多事件，新唯一约束应使迁移失败；须先依据业务事实确定 ordinal，禁止按 UUID 或时间自动猜序。

这里保证正常投递中前驱确认后才领取后继；租约失效造成的迟到重复请求，仍需消费者幂等处理。

**5. 上限与死信：采纳默认 8 次、指数退避、不自动复活。**

按上述计数定义，第 `n` 次领取失败后的退避为：

```text
min(base × 2^(n−1), cap)
```

首次失败等待 2 秒；第 8 次失败直接死信，不再安排自动领取。计算须避免指数溢出。

**死信查询与告警属于本切片必需项**，至少能看到租户、聚合、事件、次数、失败原因、死信时间及被阻塞情况；不要求本切片建设运维页面。有限重试后的最终送达依赖人工处置，不能宣称自动保证最终送达。

**6. transport 未配置：采纳启动失败，不增加本地落盘队列。**

配置缺失或无受支持 transport 时拒绝启动，且不领取事件。运行期间临时不可用走重试和死信。

只有符合传输契约的成功确认才能设置 `published_at`；HTTP 返回码的成功范围必须明确，不能将任意响应视为送达。

**7. 重放与幂等：死信重放采纳，清空 `published_at` 限定为显式修复操作。**

死信重放须原子清空 `dead_lettered_at`、`retry_count`、租约及错误，令 `next_attempt_at` 立即到期；保留原事件 ID、顺序和封套内容，并记录操作者及原因。

普通重放不得重置已发布事件。确需重新投递已发布事件时，须显式授权，并暂停、排空该聚合的在途投递后操作；这不能恢复已经发生过的历史消费顺序。

HTTP 使用：

```http
Idempotency-Key: <event_id>
```

消费者仍按 `(tenant_id,event_id)` 去重。相同 ID 重放不应重新产生业务副作用；若目标是重建消费结果，需要消费者另行提供重建流程。

【必须改的 DDL】

`0006` 归本切片；不修改已应用迁移。以下在无历史顺序歧义时可直接执行；新增唯一约束会拒绝有歧义的数据。

```sql
ALTER TABLE outbox_events
  ADD COLUMN trace_id text,
  ADD COLUMN event_ordinal integer NOT NULL DEFAULT 0,
  ADD COLUMN next_attempt_at timestamptz NOT NULL DEFAULT now(),
  ADD COLUMN locked_until timestamptz,
  ADD COLUMN delivery_token uuid,
  ADD COLUMN last_error text,
  ADD COLUMN dead_lettered_at timestamptz,

  ADD CONSTRAINT ck_outbox_trace_id_nonblank
    CHECK (trace_id IS NULL OR length(btrim(trace_id)) > 0),

  ADD CONSTRAINT ck_outbox_event_ordinal
    CHECK (event_ordinal >= 0),

  ADD CONSTRAINT ck_outbox_lease_pair
    CHECK (
      (locked_until IS NULL AND delivery_token IS NULL)
      OR
      (locked_until IS NOT NULL AND delivery_token IS NOT NULL)
    ),

  ADD CONSTRAINT ck_outbox_last_error_length
    CHECK (last_error IS NULL OR char_length(last_error) <= 500),

  ADD CONSTRAINT ck_outbox_terminal_exclusive
    CHECK (published_at IS NULL OR dead_lettered_at IS NULL),

  ADD CONSTRAINT ck_outbox_terminal_without_lease
    CHECK (
      (published_at IS NULL AND dead_lettered_at IS NULL)
      OR
      (locked_until IS NULL AND delivery_token IS NULL)
    ),

  ADD CONSTRAINT uq_outbox_aggregate_sequence
    UNIQUE (
      tenant_id,
      aggregate_type,
      aggregate_id,
      aggregate_version,
      event_ordinal
    );

COMMENT ON COLUMN outbox_events.trace_id IS
  '事件产生时的上游 trace；NULL 时封套使用事件 id，不表示上游请求 trace';

COMMENT ON COLUMN outbox_events.event_ordinal IS
  '同一聚合版本内由生产者确定的事件顺序；单事件默认 0';

COMMENT ON COLUMN outbox_events.retry_count IS
  '已领取的投递尝试次数；领取时增加，包含领取后崩溃的尝试';

COMMENT ON COLUMN outbox_events.delivery_token IS
  '每次领取生成的新令牌；确认、失败及续租须校验令牌和有效租约';

DROP INDEX idx_outbox_pending;

CREATE INDEX idx_outbox_pending
  ON outbox_events (next_attempt_at, occurred_at, id)
  WHERE published_at IS NULL AND dead_lettered_at IS NULL;

CREATE INDEX idx_outbox_aggregate_order
  ON outbox_events (
    tenant_id,
    aggregate_type,
    aggregate_id,
    aggregate_version,
    event_ordinal
  )
  WHERE published_at IS NULL;
```

平台创建角色后，投递权限为：

```sql
GRANT SELECT ON outbox_events TO fkh_relay;

GRANT UPDATE (
  published_at,
  retry_count,
  next_attempt_at,
  locked_until,
  delivery_token,
  last_error,
  dead_lettered_at
) ON outbox_events TO fkh_relay;
```

人工重放权限不得授给普通业务角色。回滚脚本须删除新增索引、约束、列并还原原 pending 索引；执行回滚前停止并排空 relay，避免运行中的 worker 使用已删除字段。

【必须补的测试】

1. **权限隔离**：业务角色未设置租户时查询为零行；设置租户后只能读本租户。relay 可读两个租户，但修改 `payload` 或 `tenant_id` 必须报权限错误。
2. **并发领取**：两个独立连接同时领取，同一有效租约只能返回给一个 worker；同聚合只领取最早事件，其他聚合可继续领取。
3. **过期令牌**：A 租约过期后由 B 重领；A 的确认、失败、续租均更新零行，且不能覆盖 B 的状态。
4. **业务顺序**：构造时间相同、UUID 顺序与业务顺序相反的事件，仍按版本及 ordinal 领取；前驱退避、持租约或死信时，后继均不可领取。同版本 ordinal 重复必须违反唯一约束。
5. **重试预算**：首次失败退避为 base，达到 cap 后不再增长；第 8 次失败进入死信。第 8 次领取后崩溃，也须在租约到期后进入死信，不得永久悬挂。
6. **封套稳定性**：NULL trace 输出事件 ID；空白 trace 被数据库拒绝；封套通过现有严格 schema 校验，重试保持相同 ID、时间及内容。
7. **传输故障**：未配置 transport 时启动失败且数据库状态不变；模拟接收端成功后 worker 在确认前崩溃，允许再次发送，但消费者业务副作用只能发生一次。
8. **人工重放**：死信重放重置次数并立即可领，事件 ID 不变；重复消费无新增副作用；普通重放入口拒绝重置已发布事件。

【可延后】

- 写入点贯通真实上游 trace。
- 运维页面、批量重放界面；死信查询、告警和重放审计不可延后。
- 自动化归档、清理及吞吐优化。

若当前存在同版本多事件，其 ordinal 写入及历史数据处理是本切片前置条件，不可延后。

【风险提示】

**最容易上线后咬人的是：HTTP 已成功，但确认前租约过期，旧 worker 与新 worker 都完成了发送。** 数据库领取令牌解决不了接收端副作用；消费者必须将幂等去重记录与业务变更放在同一事务内。