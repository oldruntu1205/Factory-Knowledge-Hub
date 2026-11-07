-- 0021 回滚：删除导师授权台账
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM training_mentor_scopes LIMIT 1) THEN
    RAISE EXCEPTION 'FKH_ROLLBACK_BLOCKED: 导师授权台账非空，回滚会导致签核失去授权依据';
  END IF;
END $$;

DROP POLICY IF EXISTS tenant_isolation ON training_mentor_scopes;
DROP TABLE IF EXISTS training_mentor_scopes;
