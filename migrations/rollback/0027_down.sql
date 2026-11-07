-- 0027 回滚：恢复级联删除（会丢草稿痕迹，仅在确认无需审计时使用）
ALTER TABLE experience_drafts DROP CONSTRAINT IF EXISTS experience_drafts_tenant_id_transcript_id_fkey;
ALTER TABLE experience_drafts
  ADD CONSTRAINT experience_drafts_tenant_id_transcript_id_fkey
  FOREIGN KEY (tenant_id, transcript_id)
  REFERENCES interview_transcripts (tenant_id, transcript_id)
  ON DELETE CASCADE;
