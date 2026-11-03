# outbox 投递（relay）

> 对应 L1 §7.2；实现依据 2026-11-03 L1 判决（`_evidence/l1_reply_outbox_20261103.md`）。
> 代码：`src/fkh/workers/outbox_relay.py`、运行时入口 `src/fkh/workers/outbox_runner.py`；
> schema：`migrations/0006_outbox_delivery.sql`；角色：`deploy/sql/relay_role.sql`。

## 1. 目标与不做的事

业务状态（发布/撤销）与 outbox 写入在同一事务完成，事件由独立 relay 异步投递，保证**至少一次**。
relay 只负责把事件送出去并把「送没送成」落回数据库；它**不**做内容生成、不生成索引、不做去重决策。

明确不做：
- 不保证「恰好一次」。HTTP 已经成功但确认前崩溃时，事件会被再发一次；
- 不在传输层之外另建本地队列。落盘队列会把一致性责任搬到一个没人监控的文件上；
- 不自动复活死信。死信需要人工重放，因为「为什么发不出去」通常需要人判断。

## 2. 数据模型（0006 新增）

| 列 | 含义 |
|---|---|
| `trace_id` | 上游 trace；空时封套用事件 id 填充（见 §4） |
| `event_ordinal` | 同一聚合版本内的事件业务顺序，生产者写入 |
| `next_attempt_at` | 下次可领取时间（退避） |
| `locked_until` / `delivery_token` | 租约到期时间 / 本次领取者令牌，二者同生同灭 |
| `last_error` | 最近一次失败原因，≤500 字符 |
| `dead_lettered_at` | 死信时间；与 `published_at` 互斥 |

`retry_count` 的语义在 0006 中被明确为**已领取的投递尝试次数**（领取时 +1，失败时不再 +1）。
这样「领完就崩」也消耗预算：第 N 次领取后进程崩溃的事件，会在租约过期后被结算为死信，
而不是永远停在「有租约、无人确认」的状态。

约束（全部由数据库兜底）：

- `ck_outbox_lease_pair`：租约与令牌必须同时存在或同时为空；
- `ck_outbox_terminal_exclusive`：已发布与已死信互斥；
- `ck_outbox_terminal_without_lease`：终态不得继续持有租约；
- `ck_outbox_trace_id_nonblank`：trace 不能是空串（封套 `minLength:1`）；
- `uq_outbox_aggregate_sequence`：`(tenant, aggregate_type, aggregate_id, aggregate_version, event_ordinal)` 唯一。

## 3. 领取 / 确认 / 失败

领取在一个短事务内完成并**立即提交**，租约从提交时刻开始计时：

```text
next_attempt_at <= clock_timestamp()
locked_until IS NULL OR locked_until < clock_timestamp()
retry_count < max_attempts
NOT EXISTS 同聚合更早的未发布事件
FOR UPDATE SKIP LOCKED
→ SET locked_until = clock_timestamp() + lease, delivery_token = gen_random_uuid(), retry_count + 1
```

确认、失败、进入死信都必须带同一组守卫：

```text
id = :event_id AND delivery_token = :token AND locked_until > clock_timestamp()
AND published_at IS NULL AND dead_lettered_at IS NULL
```

**更新零行就是租约失效**：该 worker 必须放弃，不得再写这条事件。它挡不住已经发出的 HTTP 请求，
所以重复投递仍由消费者按 `(tenant_id, event_id)` 去重。

时间函数统一用 `clock_timestamp()` 而不是 `now()`：`now()` 是**事务开始时间**，
长连接或长事务下它会落后于真实时间，表现为「事件明明到期却领不到」。

退避：`min(base × 2^(n−1), cap)`，默认 base=2s、cap=300s、max_attempts=8。
第 8 次失败直接死信，不再安排自动领取。

## 4. 顺序：按业务阶段，不按时间

`occurred_at` 在同一事务内恒定，UUID 随机 —— 二者都不能表达业务顺序。
顺序键是 `(aggregate_version, event_ordinal)`，由生产者在写入时给出：

| 事件 | ordinal |
|---|---|
| `knowledge.version.approved.v1` | 0 |
| `knowledge.version.published.v1` | 1 |
| `knowledge.version.revoked.v1` | 2 |

映射表在 `src/fkh/domain/review.py::VERSION_EVENT_ORDINAL`；**未登记的事件类型直接报错**，
不允许落到默认 0 —— 那会让「发布」与「撤销」抢同一个顺序位，撤销被唯一约束拒绝
（回归用例：`tests/e2e/test_outbox_ordering.py`）。

前驱判定包含退避中、已领取、已死信的事件：**死信会阻塞同聚合的后续事件**，不同聚合继续推进。
这是有意的选择：宁可让该聚合停住等人工处理，也不能让撤销先于发布被投递。

`trace_id` 为空时封套填 `event_id`。此时它表达「事件自身链路」，**不代表上游请求 trace**；
重试与重放复用同一封套，不得临时生成新的 trace。

## 5. 权限模型

relay 需要跨租户扫描，而 `outbox_events` 是 `FORCE ROW LEVEL SECURITY`。做法是平台角色 `fkh_relay`：

```sql
GRANT SELECT ON outbox_events TO fkh_relay;
GRANT UPDATE (published_at, retry_count, next_attempt_at, locked_until,
              delivery_token, last_error, dead_lettered_at) ON outbox_events TO fkh_relay;
GRANT INSERT ON outbox_replays TO fkh_relay;
```

- 不授予改 `tenant_id` / `event_type` / `payload` / `aggregate_*` 的权限：拿到这些等于能伪造事件；
- 业务角色 `fkh_app` 在未设置 `app.tenant_id` 时对 outbox 零行可见（有测试）；
- 角色 DDL 只在 `deploy/sql/relay_role.sql`，迁移不建角色（角色是集群级对象，回滚不删角色）。

## 6. 配置与启动

| 环境变量 | 说明 |
|---|---|
| `FKH_DATABASE_URL` | 必须是 `fkh_relay` 的登录串；用业务串会静默扫到零行 |
| `FKH_OUTBOX_TRANSPORT` | 目前仅支持 `webhook`；其他值（含默认 `none`）⇒ **拒绝启动** |
| `FKH_OUTBOX_WEBHOOK_URL` | 接收端地址 |
| `FKH_OUTBOX_TIMEOUT_SECONDS` | 单次 HTTP 超时，默认 10 |
| `FKH_OUTBOX_BATCH_SIZE` | 单轮领取条数，默认 20 |
| `FKH_OUTBOX_LEASE_SECONDS` | 租约时长，默认 30（必须覆盖等待 + 请求耗时） |
| `FKH_OUTBOX_MAX_ATTEMPTS` | 默认 8 |
| `FKH_OUTBOX_BACKOFF_BASE_SECONDS` / `_CAP_SECONDS` | 默认 2 / 300 |

```bash
uv run python -m fkh.workers.outbox_runner --once     # 单轮
uv run python -m fkh.workers.outbox_runner            # 常驻，默认 5s 轮询
```

Webhook 成功码是**白名单** `{200, 201, 202, 204}`，不是「2xx 就算过」：
若接收端把未知路由兜底成 302 或 404 却返回成功语义，事件会被标成已发布并从重试视野里消失。
请求头带 `Idempotency-Key: <event_id>`。

## 7. 死信与人工重放

```python
from fkh.workers.outbox_relay import dead_letters, dead_letter_count, replay_dead_letter

dead_letters(conn)                     # tenant / 聚合 / 事件 / 次数 / 原因 / 死信时间 / 是否阻塞后继
dead_letter_count(conn)                # 供告警轮询的计数
replay_dead_letter(conn, tenant_id=..., event_id=..., actor_id=..., reason="...")
```

重放清空 `dead_lettered_at` / `retry_count` / 租约 / 错误，`next_attempt_at` 置为当前，
事件 id 与封套内容保持不变；同时写 `outbox_replays`（追加式，触发器禁止 UPDATE/DELETE）。
**已发布事件不允许走这个入口**：重投已发布事件会改变历史消费顺序，需要单独的显式修复流程。

## 8. 已知限制与残余风险

1. **最危险的一条**：HTTP 已成功、确认前租约过期 ⇒ 旧 worker 与新 worker 都会发送。
   `delivery_token` 只保护数据库，不保护接收端。消费者必须把幂等去重记录与业务变更放在**同一事务**。
2. 告警只到「日志 + 计数」：`dead_letter_count()` 需接入实际告警通道（本地无监控栈，未接线）。
3. 运维页面、批量重放未做；死信查询与重放审计已具备。
4. 上游真实 `trace_id` 尚未贯通（写入点仍写 NULL，封套降级为事件 id）。
5. 没有消费者实现：本仓库只保证「发出去」，接收端不在范围内。
6. 吞吐未优化：单进程串行投递，未做并发/分区；按 L1 判决属可延后项。

## 9. L1 判决落实登记（2026-11-03）

| 判决 | 落实 |
|---|---|
| 1. 专用 `fkh_relay`（BYPASSRLS，列级 UPDATE） | `deploy/sql/relay_role.sql`；测试断言业务角色零行、relay 不能改 payload/tenant_id |
| 2. 租约 + `delivery_token`，确认/失败/续租校验令牌 | `locked_until` + `delivery_token` + 统一守卫；过期后旧 worker 确认返回 `lease_lost` |
| 2'. `retry_count` 在领取时 +1 | 领取 SQL 内 `retry_count + 1`；崩溃场景由 `recover_expired()` 结算死信 |
| 3. `trace_id` 降级为事件 id | `ClaimedEvent.envelope()`；非空校验在数据库 |
| 4. 顺序改为 `(aggregate_version, event_ordinal)` | 新增列 + 唯一键 + 前驱判定；生产者侧 `VERSION_EVENT_ORDINAL` |
| 5. 8 次上限、指数退避、不自动复活 | `RelayConfig.backoff_seconds()`；死信列表 + 计数 |
| 6. 传输未配置即拒绝启动 | `build_transport()` 抛 `TransportNotConfigured`；不写任何状态 |
| 7. 重放清空死信并留审计 | `replay_dead_letter()` + `outbox_replays`（追加式） |

未采纳或偏离：无。判决中「死信查询、告警和重放审计不可延后」三项中，
查询与审计已实现，**告警通道接线仍未做**（见 §8 第 2 条）。
