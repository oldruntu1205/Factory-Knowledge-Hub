# 目录规则：实例配置（ADR-009 落地）

> 2026-11-08，迁移 `0036`。此前货号正则 `^[A-Z]{1,3}[0-9]{5,8}$` 与货币 `CNY` 写死在
> 库与应用里，条码类货号（`SKU-2026-001`）、纯数字 EAN、USD 报价都无法入库。

## 1. 模型

| 项 | 规则 |
|---|---|
| 存放 | `catalog_rules(tenant_id, code_pattern, currencies[], updated_at)`，每实例一行 |
| 引用 | 不建外键（规则是业务口径，不是引用完整性） |
| 库层 | 只做**通用形状**：货号 1–64 字符且无空白；货币 `^[A-Z]{3}$` |
| 应用层 | 统一用 Python `re.fullmatch`；规则由 `catalog_rules` 加载，**所有目录写入路径共用**：入库抽取、冲突决议、回填 |
| 并发 | 写入路径读规则时取 **FOR SHARE** 并持有至事务结束；PUT 先取 **FOR UPDATE** 再检查存量 → 二者互斥（`test_rule_writes_and_updates_are_mutually_exclusive`）；PUT 要求 **READ COMMITTED**（否则拒绝），保证等待锁之后能看到等待期间提交的数据（`test_put_sees_writes_committed_while_it_waited`；用例先证明 PUT 的 backend pid 在 `pg_locks` 中**有未授予锁**，再提交写入） |
| 缺规则 | 目录入库 → **422 可识别配置错误**（提示 `PUT /v1/config/catalog-rules`）；**读取与作答不受影响** |
| 审计 | 每次实际变更写 `catalog_rule_revisions`（前后规则、操作者、版本号）；相同内容 PUT 不产生版本 |

## 2. 端点

| 端点 | scope | 语义 |
|---|---|---|
| `GET /v1/config/catalog-rules` | `knowledge.read` | 返回 `{configured, rule}`；未配置时 `configured=false` |
| `PUT /v1/config/catalog-rules` | `knowledge.write` | 整体替换，幂等；`changed=false` 表示无变更；收紧规则使存量数据失效 → **409**；过宽规则（能匹配 `1`/`abc`/空串）→ 200 + `warnings` 提示（不拒绝） |

## 3. 收紧/放宽语义

- **收紧**（新规则不覆盖存量货号或货币）→ 409，规则保持原样，先处置数据再改；
- **放宽**（覆盖现有数据）→ 直接成功；
- 幂等：同内容重复 PUT → 200 + `changed=false`，不写审计版本。

## 4. 迁移与回滚

- 存量：迁移只为已有租户种子工厂默认（`^[A-Z]{1,3}[0-9]{5,8}$` + `{CNY}`），不改任何数据；
- 回滚（`0036_down.sql`）**有条件**：存在非旧格式货号（`catalog_items` + `catalog_conflict_groups`）
  或非 CNY 货币时先 `RAISE EXCEPTION FKH_ROLLBACK_BLOCKED`，检查与恢复在同一事务。

## 5. 验收

| 层次 | 证据 |
|---|---|
| 集成 | `tests/e2e/test_catalog_rules.py`（6 条，真实 PG + 业务角色 + HTTP） |
| 缺规则 | 目录入库 422；普通文档 201；`GET` 返回 `configured=false` |
| 自定义 | 规则改为条码 + USD → 导入 201，条目按新规则抽取 |
| 审计 | 两次相同 PUT → 只有 1 条 revision，含前后规则与操作者 |
| 收紧 | 存量不兼容 → 409 且规则不变；放宽 → 200 |
| 库层 | 通用形状约束仍在（业务规则不在库里） |

## 6. 查询侧识别（2026-11-08 补齐）

- 查询候选由**实例规则** `fullmatch` 判定（不再只认工厂正则），纯数字 EAN 可用；
- 数量/单位语境排除：前面出现数量词，或紧邻「数字 + 单位」（`3 个 6901…`）、
  或后面紧跟单位/金额词（`… 元`）时，不当货号；
- 仍是「恰好一个货号 + 明确询价/询规格」才接管，其余交回 RAG（不抢答）；
- 表格渲染（`answer/tables.py`）也按实例规则识别货号行；
- 验收：`tests/e2e/test_catalog_query_ean.py`（3 条：EAN 可答、数量语境不抢答、工厂租户回归）。

## 7. 剩余边界（如实记录）

- 规则变更**不影响已抽取的历史条目**（历史按当时规则抽取）；
- 回填 CLI（`catalog_backfill.py`）已改为读实例规则（缺规则/货币不允许直接报错）；
  它不再有 `choices=["CNY"]` 的硬编码。
