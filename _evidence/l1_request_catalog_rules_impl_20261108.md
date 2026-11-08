# L1 终审：目录规则实例配置（`70a3f56`）

- 日期：2026-11-08
- 风险归类：**DB schema 与迁移**、**对外契约新增**、**抽取/校验口径变化**
- 设计回执：`_evidence/l1_reply_catalog_rules_20261108.md`（有条件通过，5 条裁决 + 3 项补充约定）
- 请只做判断：**不要探索仓库、不要读源码、不要跑测试**。

## 1. 按裁决逐条落地

| 你的要求 | 实现 |
|---|---|
| 缺配置 → 拒绝目录入库，可识别配置错误；读取不受影响 | `require_rule` 抛 `CatalogRuleMissing` → 422，message 含 `PUT /v1/config/catalog-rules`；`GET` 返回 `configured=false`；普通文档（非 `product_catalog`）入库不受影响 |
| DB 只通用形状，不加触发器 | 旧枚举 CHECK 换成：货号 `length 1–64 且无空白`、货币 `^[A-Z]{3}$` |
| 所有写入路径共用校验 | 入库（`extract_catalog_items` 内部强制加载规则）、冲突决议（`resolve_group`）、回填 CLI（`catalog_backfill`）；仓库层在未传规则时自行加载，避免漏检 |
| 单一正则引擎 + 整串匹配 | Python `re` + `fullmatch`；pattern 长度上限 200；PUT/入库/解析同一 `compile_rule` |
| 纯函数显式传规则（方案 A） | `parse_catalog_text(text, code_pattern=...)`；不再在解析器内查库，也没有隐式默认（未传时使用工厂常量仅用于读路径兜底） |
| 货币集合 `text[]` | 非空、元素唯一、`^[A-Z]{3}$`，PUT 规范化排序后保存 |
| PUT 审计、原子、同内容免版本 | `catalog_rule_revisions(tenant_id, revision_no, before_rule, after_rule, actor_id, created_at)`，与更新同事务；相同内容返回 `changed=false` |
| 收紧使存量失效 → 拒绝 | `_assert_compatible` 检查 `catalog_items.code` + `catalog_conflict_groups.code` + 货币集合，任一不满足 → 409，规则不变（用例断言旧规则仍生效） |
| 并发 | `save_rule` 对 `catalog_rules` 行 `FOR UPDATE`（不存在时锁租户行）；写入路径在同一事务内校验 |
| 回滚覆盖两张表 | `0036_down` 同一事务内先检查两张货号表 + 非 CNY 货币，命中即 `RAISE EXCEPTION FKH_ROLLBACK_BLOCKED` |

## 2. 端点

| 端点 | scope | 语义 |
|---|---|---|
| `GET /v1/config/catalog-rules` | `knowledge.read` | `{configured, rule}` |
| `PUT /v1/config/catalog-rules` | `knowledge.write` | 整体替换；200 `changed`；409 收紧冲突；422 非法正则/货币 |

租户一律取自认证上下文（表是**每租户**一份）。

## 3. 验收证据

- `tests/e2e/test_catalog_rules.py`（6 条，真实 PG + 业务角色 + HTTP）：
  缺规则 422 + 普通文档 201 + `configured=false`；条码货号 + USD 成功后入库；
  幂等（两次相同 PUT → 1 条 revision，含前后规则与操作者）；收紧 409 且规则不变、放宽 200；
  货币不在集合 422；非法正则/小写货币/空集合 422。
- 全量：`uv run pytest -q` → **643 passed**（exit=0）；ruff / mypy 全绿。

## 4. 如实记录的边界（未做）

1. **查询侧货号识别**仍用通用兜底正则（`answer/tables.py` 的 `_CODE`、
   `catalog.py` 的 `_CODE_IN_QUERY`）：纯数字 EAN 可能与价格/数量混淆。
   按你的提示"正则匹配只能说明可能是货号"，这需要行列上下文改造，本片未做。
2. 规则变更**不回溯已抽取的历史条目**（历史按当时规则抽取，符合"最终写入时规则为准"）。
3. `catalog_rule_revisions` 目前只在 PUT 时写；不记录"读取/回填使用哪版规则"。

## 5. 需要你判断的三点

1. 缺规则时**普通文档（非 product_catalog）仍可入库**、目录入库被拒——这个分界是否符合你的意图？
   （我理解为"目录能力需要显式开通"，而不是阻断整个租户。）
2. 收紧冲突返回 **409** 而不是 422：409 表达"状态冲突需要先处置数据"，但公开 message 是固定文案
   （`detail` 只进日志）。是否需要把"哪些货号不合规"做成可返回的字段（新契约）？
3. 查询侧识别未改造，是否需要单独立项（会影响 `answer/tables.py` 与意图识别）？
