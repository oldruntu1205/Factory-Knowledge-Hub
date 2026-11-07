-- 0028 回滚：移除晋升链接列
DROP INDEX IF EXISTS idx_drafts_promoted_version;
ALTER TABLE experience_drafts DROP CONSTRAINT IF EXISTS experience_drafts_tenant_id_promoted_version_id_fkey;
ALTER TABLE experience_drafts DROP COLUMN IF EXISTS promoted_at;
ALTER TABLE experience_drafts DROP COLUMN IF EXISTS promoted_version_id;
