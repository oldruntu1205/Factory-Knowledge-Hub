-- 0027：撤回授权时保留草稿行（供审计），只把转写删除
--
-- 0026 里 experience_drafts.transcript_id 是 ON DELETE CASCADE：删转写会把草稿整行带走，
-- 撤回后就无法回答"当时有过哪些草稿"。改为可空 + ON DELETE SET NULL (transcript_id)，
-- 由服务层先把草稿置为 WITHDRAWN 再删转写（2026-11-08 实测）。

ALTER TABLE experience_drafts ALTER COLUMN transcript_id DROP NOT NULL;

ALTER TABLE experience_drafts DROP CONSTRAINT IF EXISTS experience_drafts_tenant_id_transcript_id_fkey;

ALTER TABLE experience_drafts
  ADD CONSTRAINT experience_drafts_tenant_id_transcript_id_fkey
  FOREIGN KEY (tenant_id, transcript_id)
  REFERENCES interview_transcripts (tenant_id, transcript_id)
  ON DELETE SET NULL (transcript_id);
