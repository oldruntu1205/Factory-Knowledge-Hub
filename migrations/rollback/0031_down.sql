-- 0031 回滚：移除快照列与 selected_candidate_id 外键
ALTER TABLE catalog_conflict_groups
  DROP CONSTRAINT IF EXISTS catalog_conflict_groups_selected_candidate_fkey;
ALTER TABLE catalog_conflict_groups DROP COLUMN IF EXISTS resolved_snapshot;
