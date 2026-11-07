-- 0026 回滚：删除经验采集模型（有数据时拒绝，避免丢失未发布素材）
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM experience_drafts LIMIT 1)
     OR EXISTS (SELECT 1 FROM interview_transcripts LIMIT 1) THEN
    RAISE EXCEPTION 'FKH_ROLLBACK_BLOCKED: 已有转写/草稿数据，回滚会丢失经验素材';
  END IF;
END $$;

DROP POLICY IF EXISTS tenant_isolation ON experience_drafts;
DROP POLICY IF EXISTS tenant_isolation ON interview_transcripts;
DROP POLICY IF EXISTS tenant_isolation ON interviews;
DROP POLICY IF EXISTS tenant_isolation ON interview_consents;

DROP TABLE IF EXISTS experience_drafts;
DROP TABLE IF EXISTS interview_transcripts;
DROP TABLE IF EXISTS interviews;
DROP TABLE IF EXISTS interview_consents;
