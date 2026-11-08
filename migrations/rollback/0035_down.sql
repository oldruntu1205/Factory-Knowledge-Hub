-- 0035 回滚：有条件回滚（L1 2026-11-08）
--
-- 一旦出现旧 8 域之外的数据，旧枚举 CHECK 无法恢复 → 先检测并拒绝，
-- 不允许"先删配置表再发现恢复不了"。

DO $$
DECLARE
  offending integer;
BEGIN
  SELECT count(*) INTO offending
    FROM knowledge_documents
   WHERE domain NOT IN (
     'owner_experience','glass_materials','production_process','product_catalog',
     'quality','equipment_safety','foreign_trade','training');

  IF offending > 0 THEN
    RAISE EXCEPTION
      'FKH_ROLLBACK_BLOCKED: 存在 % 篇使用非旧 8 域的知识文档，回滚会撞旧枚举 CHECK；请先处置这些文档',
      offending;
  END IF;
END $$;

ALTER TABLE knowledge_documents DROP CONSTRAINT IF EXISTS knowledge_documents_tenant_domain_fkey;
ALTER TABLE knowledge_documents DROP CONSTRAINT IF EXISTS knowledge_documents_domain_format_check;
ALTER TABLE knowledge_documents
  ADD CONSTRAINT knowledge_documents_domain_check
  CHECK (domain IN (
    'owner_experience','glass_materials','production_process','product_catalog',
    'quality','equipment_safety','foreign_trade','training'));

DROP POLICY IF EXISTS tenant_isolation ON knowledge_domains;
DROP TABLE IF EXISTS knowledge_domains;
