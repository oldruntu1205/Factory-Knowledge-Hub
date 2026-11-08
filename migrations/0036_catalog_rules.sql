-- 0036：目录货号规则与货币集合下沉为实例配置（ADR-009 剩余硬编码）
--
-- 背景：货号正则 `^[A-Z]{1,3}[0-9]{5,8}$` 与货币 `CNY` 写死在库里与应用里，
-- 非工厂客户（条码 SKU-2026-001、纯数字 EAN、USD 报价）无法入库。
--
-- 责任边界（L1 2026-11-08 裁决）：
--   * 数据库只保证**通用数据形状**（无触发器、不按租户正则强校验）；
--   * 应用层保证**租户业务规则**，所有目录写入路径共用同一套校验；
--   * 缺规则 → 拒绝目录入库（可识别的配置错误），**不**回落到内置工厂默认；
--   * 已有目录的读取不受缺规则影响（读路径可用通用兜底）。
--
-- 本迁移：
--   1) 建 catalog_rules（每租户一行）+ catalog_rule_revisions（变更审计）；
--   2) 为已有租户种子工厂默认（^[A-Z]{1,3}[0-9]{5,8}$ + {CNY}）；
--   3) 把两张表的 code/currency 枚举式 CHECK 换成通用格式 CHECK；
--   4) 加触发器维护 updated_at（不含业务正则校验）。

CREATE TABLE IF NOT EXISTS catalog_rules (
  tenant_id    uuid PRIMARY KEY REFERENCES tenants(id) ON DELETE RESTRICT,
  code_pattern text NOT NULL CHECK (length(code_pattern) BETWEEN 2 AND 200),
  currencies   text[] NOT NULL CHECK (cardinality(currencies) > 0),
  updated_at   timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE catalog_rules ENABLE ROW LEVEL SECURITY;
ALTER TABLE catalog_rules FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON catalog_rules
  USING (tenant_id = fkh_current_tenant())
  WITH CHECK (tenant_id = fkh_current_tenant());

CREATE TABLE IF NOT EXISTS catalog_rule_revisions (
  tenant_id   uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
  revision_no bigint NOT NULL,
  before_rule jsonb,
  after_rule  jsonb NOT NULL,
  actor_id    uuid,
  created_at  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, revision_no),
  FOREIGN KEY (tenant_id, actor_id) REFERENCES principals (tenant_id, id)
);

ALTER TABLE catalog_rule_revisions ENABLE ROW LEVEL SECURITY;
ALTER TABLE catalog_rule_revisions FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON catalog_rule_revisions
  USING (tenant_id = fkh_current_tenant())
  WITH CHECK (tenant_id = fkh_current_tenant());

-- 已有租户：工厂默认规则（保持历史行为）
INSERT INTO catalog_rules (tenant_id, code_pattern, currencies)
SELECT id, '^[A-Z]{1,3}[0-9]{5,8}$', ARRAY['CNY']
  FROM tenants
ON CONFLICT (tenant_id) DO NOTHING;

-- 通用格式约束（业务规则交给应用层）
ALTER TABLE catalog_items DROP CONSTRAINT IF EXISTS catalog_items_code_check;
ALTER TABLE catalog_items
  ADD CONSTRAINT catalog_items_code_shape_check
  CHECK (length(code) BETWEEN 1 AND 64 AND code !~ '\s');

ALTER TABLE catalog_items DROP CONSTRAINT IF EXISTS catalog_items_currency_check;
ALTER TABLE catalog_items
  ADD CONSTRAINT catalog_items_currency_shape_check
  CHECK (currency ~ '^[A-Z]{3}$');

ALTER TABLE catalog_conflict_groups DROP CONSTRAINT IF EXISTS catalog_conflict_groups_code_check;
ALTER TABLE catalog_conflict_groups
  ADD CONSTRAINT catalog_conflict_groups_code_shape_check
  CHECK (length(code) BETWEEN 1 AND 64 AND code !~ '\s');

COMMENT ON TABLE catalog_rules IS
  '实例（租户）级目录规则：货号正则 + 允许货币；应用层所有目录写入路径共用此校验，库层只保证通用形状';
