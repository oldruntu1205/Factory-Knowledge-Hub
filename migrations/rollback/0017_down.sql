-- 回滚 0017：删除一致性检查审计列。
--
-- 按既有纪律：这些列是"为什么把回答降级为复核"的唯一记录，删掉就再也解释不清。
-- 因此**有任何一行填过就拒绝**；确认可以丢弃后再手工执行 DROP。
DO $$
DECLARE
  used integer;
BEGIN
  SELECT count(*) INTO used FROM rag_queries WHERE consistency_rule IS NOT NULL;
  IF used > 0 THEN
    RAISE EXCEPTION
      'FKH_ROLLBACK_BLOCKED: 有 % 行记录带一致性检查结果，删除后无法复现降级理由；'
      '请先确认可丢弃这些审计依据',
      used
      USING ERRCODE = '23514';
  END IF;
END;
$$;

ALTER TABLE rag_queries DROP CONSTRAINT IF EXISTS ck_rag_consistency_counts;
ALTER TABLE rag_queries
  DROP COLUMN IF EXISTS consistency_rule,
  DROP COLUMN IF EXISTS codes_total,
  DROP COLUMN IF EXISTS codes_supported,
  DROP COLUMN IF EXISTS numbers_total,
  DROP COLUMN IF EXISTS numbers_supported,
  DROP COLUMN IF EXISTS consistency_ms;
