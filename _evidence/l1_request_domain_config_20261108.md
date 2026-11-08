# L1 设计评审：知识域改为实例配置（去工厂硬编码）

- 日期：2026-11-08
- 风险归类：**DB schema 与迁移**、**对外契约（新增配置端点）**、**可见性/校验口径**
- 背景：ADR-009 确认"单实例私域部署、一客户一套实例"；实测发现**知识域枚举写死**是唯一
  "必须改内核才能用"的复用阻塞：用 `bakery_equipment` 上传 → 422
  （`knowledge_documents.domain` 的 CHECK + `VALID_DOMAINS` 都是 8 个工厂域）。
- 请只做判断：**不要探索仓库、不要读源码、不要跑测试**。

## 1. 现状

- `knowledge_documents.domain` 有 CHECK 限定 8 个值：
  `owner_experience / glass_materials / production_process / product_catalog / quality /
   equipment_safety / foreign_trade / training`
- API 层 `VALID_DOMAINS` 再校验一次（同 8 个）。
- 域只用于**组织与展示**（列表过滤、演示分组），不参与可见性判定（可见性由 status /
  audience / required_scope / ACL 决定）。

## 2. 拟议方案

### 2.1 新表（每租户一份域清单）

```sql
CREATE TABLE knowledge_domains (
  tenant_id   uuid NOT NULL REFERENCES tenants(id),
  code        text NOT NULL CHECK (code ~ '^[a-z][a-z0-9_]{1,63}$'),
  label       text NOT NULL,
  is_active   boolean NOT NULL DEFAULT true,
  is_default  boolean NOT NULL DEFAULT false,   -- 工厂默认 8 个
  created_at  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, code)
);
```

迁移时：
1. 为**每个已有租户**插入 8 个默认域（`is_default=true`）；
2. 把 `knowledge_documents.domain` 的 CHECK 从"枚举"改为"格式"（`^[a-z][a-z0-9_]{1,63}$`）；
3. **不删列、不改已有值**，所以存量数据零迁移成本、可回滚。

### 2.2 校验位置

- **应用层**（上传/更新时）：`domain` 必须在该租户 `knowledge_domains` 中且 `is_active`；
  否则 422 + hint「domain 未在该实例登记，可通过 POST /v1/config/domains 添加」。
- **数据库层**：只做格式校验，不建 FK。理由：
  - 单实例私域下"域"是**运营配置**，不是强一致约束；
  - FK 会让"新租户首篇文档"必须先建域，把建租户流程与迁移绑死；
  - 域不参与可见性，格式合法 + 应用层校验已足够防止"笔误导致的孤儿域"。

### 2.3 端点

| 端点 | scope | 说明 |
|---|---|---|
| `GET /v1/config/domains` | `knowledge.read` | 列出本实例域清单 |
| `POST /v1/config/domains` | `knowledge.write` | 登记新域（code/label），幂等 |

不提供删除/停用端点（本轮）：停用会与"历史文档仍引用该域"冲突，留待明确口径。

### 2.4 与既有行为的关系

- 8 个工厂域仍可用（迁移种子）；
- 现有测试里"`bakery_equipment` → 422"的断言必须**改为**：登记后可用、未登记仍 422。
  这属于契约强制变更，我会在测试与提交信息中标注原因（不放宽断言）。
- 演示实例（A）不受影响；非工厂实例（B）需在导入前登记自己的域，或由种子预置。

## 3. 需要你判断的五点

1. **不建 FK、只做格式校验 + 应用层清单**是否可接受？
   还是要求 `knowledge_documents(tenant_id, domain)` 外键到 `knowledge_domains`（强一致，但建租户流程要随之调整）？
2. **默认 8 域是否应写入新租户**？现在没有"建租户"API（由种子/测试直接 INSERT），
   我拟在迁移里为已有租户种子，并在应用层"首次登记域"时按需创建；
   若你认为必须"租户一建立就有默认域"，需要在租户创建处加钩子（范围更大）。
3. **域不参与可见性**这一点你确认吗？（我理解域只是分类标签，权限由 scope/ACL/状态决定；
   若未来要"按域授权"，那需要另一套设计。）
4. **`POST /v1/config/domains` 需要 `knowledge.write`** 是否合适，
   还是要独立 scope（如 `config.domain.write`）？
5. **停用/删除域**本轮不做是否可接受？

## 4. 边界

- 本片不动：可见性谓词、检索、引用、撤销、索引闸门、培训/访谈链路；
- 本片不改：`knowledge_documents` 的其它列与索引；
- 回滚：删表 + 恢复旧 CHECK（若已有非 8 域数据，回滚会被 CHECK 拒绝，这是期望行为）。
