# L1 设计评审：目录货号规则与货币集合下沉为实例配置

- 日期：2026-11-08
- 风险归类：**DB schema 与迁移**、**对外契约（配置端点 + 上传校验）**、**抽取口径**
- 背景：ADR-009 落地知识域后（`0035`），复用清单剩两项非阻塞但真实的工厂专属硬编码：
  1. 货号正则 `^[A-Z]{1,3}[0-9]{5,8}$`（DB CHECK ×2 + 抽取/问答解析 `answer/tables.py`）；
  2. 目录货币只允许 `CNY`（DB CHECK + `IngestionRequest` 校验 + 两个 CLI 的 `choices`）。
  非工厂客户（条码 `SKU-2026-001`、纯数字 EAN、USD 报价）目前无法入库。
- 请只做判断：**不要探索仓库、不要读源码、不要跑测试**。

## 1. 现状细节

| 位置 | 内容 |
|---|---|
| DB `catalog_items.code` | `CHECK (code ~ '^[A-Z]{1,3}[0-9]{5,8}$')` |
| DB `catalog_conflict_groups.code` | 同上（冲突待办按货号建组） |
| DB `catalog_items.currency` | `CHECK (currency IN ('CNY'))` |
| 抽取/渲染 | `answer/tables.py` 用 `^[A-Z]{1,3}\d{5,8}$` 判定"这一行是货号行" |
| 入库校验 | `IngestionRequest`：`catalog_currency not in {'CNY'}` → ValueError |
| CLI | `catalog_backfill.py` / `answer_regression.py` 的 `--currency choices=["CNY"]` |

## 2. 拟议方案

### 2.1 配置表（每实例一份）

```sql
CREATE TABLE catalog_rules (
  tenant_id    uuid PRIMARY KEY REFERENCES tenants(id),
  code_pattern text NOT NULL,              -- 正则，例如 ^[A-Z]{1,3}[0-9]{5,8}$
  currencies   text[] NOT NULL,            -- 例如 {CNY} 或 {CNY,USD}
  updated_at   timestamptz NOT NULL DEFAULT now()
);
```

迁移：为**每个已有租户**插入工厂默认（正则 + `{CNY}`）；新租户无行 → 应用层回落到
"内置保守默认"还是"拒绝入库"？我倾向**拒绝**（与域一致：显式配置后才可用），但会让
新建租户的全部目录入库被拒——除非部署脚本显式写规则。请判定。

### 2.2 DB 约束

- `catalog_items.code` / `catalog_conflict_groups.code`：枚举式正则 → **通用格式**
  （`length 1..32`、无空白）；
- `catalog_items.currency`：`IN ('CNY')` → `^[A-Z]{3}$`；
- 每租户正则/货币集合由**应用层**校验（抽取、冲突决议、入库、回填 CLI）。

**问题**：是否需要 DB 层按租户规则强校验（触发器读 `catalog_rules`）？
我认为触发器可行（`BEFORE INSERT/UPDATE` 用 `catalog_rules.code_pattern` 做 `~` 匹配），
但与知识域不同：那是引用完整性（FK 天然表达），这是**正则匹配**，库层做也可以但成本更高、
迁移/回滚更重。请裁决：**触发器 vs 仅应用层**。

### 2.3 端点

| 端点 | scope | 说明 |
|---|---|---|
| `GET /v1/config/catalog-rules` | `knowledge.read` | 读本实例规则（pattern + currencies） |
| `PUT /v1/config/catalog-rules` | `knowledge.write` | 整体替换（幂等）；校验 pattern 可编译、currencies 为 3 位大写 |

### 2.4 抽取与解析的关系

- `answer/tables.py` 目前用固定正则判断"货号行"。改造后需按实例规则判断，
  但它当前是**纯函数**（无 DB 访问）。
  - 方案 A：把 `code_pattern` 作为参数传入（调用方从 `catalog_rules` 取）；
  - 方案 B：保留固定正则作为"通用兜底"，只有配置了自定义规则时才覆盖。
  我倾向 A（显式），但这会改动若干调用点（抽取/问答/回填）。
  请判定是否可接受 A 的改动面，或要求 B。

### 2.5 兼容与回滚

- 迁移不重写任何存量数据（旧货号都满足新通用格式）；
- 回滚（`0036_down`）：若存在不匹配旧正则的货号或非 CNY 货币，先检测并 `RAISE`（同 0035 做法）。

## 3. 需要你判断的五点

1. **新租户无 `catalog_rules` 行时**：拒绝目录入库（显式配置）还是回落内置默认（`^[A-Z]{1,3}[0-9]{5,8}$` + CNY）？
   我倾向拒绝，与知识域口径一致。
2. **DB 层是否要触发器**按租户正则强校验，还是只做通用格式 + 应用层？
3. **`answer/tables.py` 的纯函数改造**：接受方案 A（显式传 pattern）还是 B（兜底 + 覆盖）？
4. **货币集合**：`text[]` 是否可接受，还是要独立表 `catalog_currencies(tenant_id, code)`（便于未来加汇率/小数位）？
5. **PUT 整体替换**：是否需要保留历史（规则变更审计）？若需要，我加 `catalog_rule_revisions`，
   但本轮范围会扩大。

## 4. 边界

- 不动检索/可见性/引用/撤销/索引闸门；
- 不改价签解析逻辑（价格仍是数字 + 货币码）；
- 回填 CLI 的 `choices=["CNY"]` 改为读实例规则。
