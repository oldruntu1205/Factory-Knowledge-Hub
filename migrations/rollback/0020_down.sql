-- 0020 回滚：删除正文读取审计表
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM training_content_access_log LIMIT 1) THEN
    RAISE EXCEPTION 'FKH_ROLLBACK_BLOCKED: 审计表中已有正文读取记录，回滚会丢失审计';
  END IF;
END $$;

DROP POLICY IF EXISTS tenant_isolation ON training_content_access_log;
DROP TABLE IF EXISTS training_content_access_log;
