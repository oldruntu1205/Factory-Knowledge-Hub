-- 0036 回滚：有条件回滚（L1 2026-11-08）
--
-- 旧 CHECK 只认 ^[A-Z]{1,3}[0-9]{5,8}$ 与 CNY。若已存在不合规的货号/货币，
-- 恢复旧约束会失败——先检测并拒绝，检查与恢复在同一事务内。

DO $$
DECLARE
  bad_codes bigint;
  bad_currency bigint;
BEGIN
  SELECT count(*) INTO bad_codes FROM (
    SELECT code FROM catalog_items WHERE code !~ '^[A-Z]{1,3}[0-9]{5,8}$'
    UNION ALL
    SELECT code FROM catalog_conflict_groups WHERE code !~ '^[A-Z]{1,3}[0-9]{5,8}$'
  ) AS bad;

  SELECT count(*) INTO bad_currency FROM catalog_items WHERE currency <> 'CNY';

  IF bad_codes > 0 OR bad_currency > 0 THEN
    RAISE EXCEPTION
      'FKH_ROLLBACK_BLOCKED: 存在 % 条非旧格式货号、% 条非 CNY 货币，恢复旧约束会失败；请先处置这些数据',
      bad_codes, bad_currency;
  END IF;
END $$;

ALTER TABLE catalog_conflict_groups DROP CONSTRAINT IF EXISTS catalog_conflict_groups_code_shape_check;
ALTER TABLE catalog_conflict_groups
  ADD CONSTRAINT catalog_conflict_groups_code_check
  CHECK (code ~ '^[A-Z]{1,3}[0-9]{5,8}$');

ALTER TABLE catalog_items DROP CONSTRAINT IF EXISTS catalog_items_code_shape_check;
ALTER TABLE catalog_items
  ADD CONSTRAINT catalog_items_code_check CHECK (code ~ '^[A-Z]{1,3}[0-9]{5,8}$');

ALTER TABLE catalog_items DROP CONSTRAINT IF EXISTS catalog_items_currency_shape_check;
ALTER TABLE catalog_items
  ADD CONSTRAINT catalog_items_currency_check CHECK (currency = 'CNY');

DROP POLICY IF EXISTS tenant_isolation ON catalog_rule_revisions;
DROP TABLE IF EXISTS catalog_rule_revisions;
DROP POLICY IF EXISTS tenant_isolation ON catalog_rules;
DROP TABLE IF EXISTS catalog_rules;
