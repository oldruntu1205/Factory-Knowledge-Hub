-- 0034 回滚：移除快照结构约束（退回 0033 的宽松口径）
ALTER TABLE catalog_conflict_groups
  DROP CONSTRAINT IF EXISTS catalog_conflict_groups_keep_snapshot_check;
