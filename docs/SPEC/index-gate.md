# 索引发布闸门与撤销收敛（Rev2 §7.2 / §8.4）

> 2026-11-08 落地（迁移 `0022`、`0023`、`0024`）。对应验收：`tests/e2e/test_index_gate.py`（4 条，真实 PG + HTTP）。

## 1. 两道表与语义

| 表 | 作用 |
|---|---|
| `index_generations(tenant_id, generation, status, chunk_count)` | 每一代索引是否 **READY**（构建完成）。状态 `BUILDING / READY / FAILED` |
| `index_visibility(tenant_id, generation)` | 租户当前**可见代水位**。检索只认 `<= 水位` 的块 |

- 入库（ingest）：切片写入后把该代标 `READY`；**不**推进水位。
- 发布（`APPROVED_INTERNAL` / `PUBLISHED_PUBLIC`）：先校验该版本的所有块所属代 `READY`，
  再原子推进水位（`GREATEST(旧, 新)`）。校验不过 → **409 `VERSION_CONFLICT`**，版本停在原状态。
- 检索（词法 / 混合 / 目录精确查询）：公开内容（`PUBLISHED_PUBLIC`）额外要求
  `index_generation <= 可见水位`；水位缺失按 0 处理（= 尚未切换，公开内容不可见）。
  内部内容（`APPROVED_INTERNAL`）不额外受水位限制，仍由状态与范围控制。

> 为什么内部内容不查水位：内部草稿/待评版本在 `DRAFT`/`REVIEWED` 阶段本来就被状态挡住；
> 加水位会让"检索测试"这类只关心词法行为的路径全部失效，属于把闸门用错了位置。
> 公开面是唯一"对外可见"的承诺，闸门放在那里。

## 2. 撤销与重建收敛

- **撤销**：状态同步改 `REVOKED` + 写 outbox 事件；API 立即拒绝（问答 / 检索 / 证据三路），
  **不等待**物理清理。此时块仍在库里（已验证）。
- **重建**（`fkh.workers.index_maintenance.rebuild_visible_generation`）：
  1. 物理删除 `REVOKED` / `DISCARDED` 版本的块；
  2. 切出新一代并标记 READY；
  3. 推进可见水位。
  验收断言：重建后该版本的块计数为 **0**，水位前移，旧引用直读仍 403（收敛）。

### 2.1 迁移 `0023` + `0024`（审计与删除的冲突）

`rag_citations` 对 `knowledge_chunks` 是复合外键 RESTRICT——历史引用会**阻止删除**，
撤销内容永远留在库里。改为 `ON DELETE SET NULL`，但复合外键默认连 `tenant_id` 一起置空，
撞 NOT NULL（实测报错）。`0024` 用 PG15+ 的列级写法 **`ON DELETE SET NULL (chunk_id)`**，
只置空块 ID，保留 `version_id`/`locator`，审计仍可回答"引用过哪个版本的哪一段"。

## 3. 消费闭环（`0025`，L1 阻断项 3 已补）

`fkh.workers.revoke_purge` 消费 `knowledge.version.revoked.v1` / `...discarded.v1`：

| 要求 | 实现 |
|---|---|
| 持久 | 事件在 `outbox_events`，消费状态在 `outbox_deliveries`（`(tenant,event_id,consumer)` 主键） |
| 幂等 | 已 `DONE` 的事件不再处理（重复消费 `processed=0`） |
| 重试 | 失败累加 `attempts` 并保持 `PENDING`，下轮再试（上限 5） |
| 死信/可追踪 | 耗尽转 `FAILED` 并记 `last_error`；`failed_deliveries()` 列出、`reset_failed()` 补偿重入 |

通用消费框架（多消费者、调度、指标）仍留 ADR-06；本片只承担撤销清理这一条责任。

## 4. 证据直读也受水位约束（L1 阻断项 4 已补）

`_CHUNK_SQL` / `_PARENT_SQL` 对 `PUBLISHED_PUBLIC` 内容追加
`index_generation <= 可见水位` 判断（用 `c.tenant_id` 关联，不改参数风格）。
验收：同一块在水位存在时 200、水位被删除后 403 —— 与检索口径一致。
内部内容（`APPROVED_INTERNAL`）仍按状态 + 范围判定。

## 5. 已知边界（诚实）

1. 消费端目前**没有常驻调度**：需要由运维/定时任务调用 `consume_revoke_events`（本片未接 cron）；
2. 缓存与签名链接：本系统尚无缓存层与签名链接，§8.4 对应条目不适用（上线前若引入需重测）；
3. 通用 outbox 框架（多消费者、指标、告警）留 ADR-06；
4. **PG15+ 成为部署前置条件**（`ON DELETE SET NULL (chunk_id)` 语法要求）。

## 4. 验收

```
uv run pytest -q tests/e2e/test_index_gate.py   → 4 passed
特化场景：
- 索引代被标 FAILED 后发布           → 409，版本仍 REVIEWED
- 发布成功                           → 水位 = 该版本代
- 水位回退（删除水位行）             → 公开检索立刻为空（旧代不泄露）
- 撤销 → 问答不可答 / 旧引用 403 / 块仍在（异步）
- 重建 → 块计数 0、水位前移、旧引用仍 403
```
