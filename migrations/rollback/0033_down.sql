-- 0033 回滚：恢复 0015 的严格 KEEP 约束（会重新阻止"候选被清理后的 KEEP"）
ALTER TABLE catalog_conflict_groups DROP CONSTRAINT IF EXISTS catalog_conflict_groups_check;
ALTER TABLE catalog_conflict_groups
  ADD CONSTRAINT catalog_conflict_groups_check CHECK (
    (status = 'OPEN'
       AND selected_candidate_id IS NULL AND resolved_by IS NULL AND resolved_at IS NULL)
    OR (status = 'KEEP'
       AND selected_candidate_id IS NOT NULL AND resolved_by IS NOT NULL AND resolved_at IS NOT NULL)
    OR (status = 'DROP_ALL'
       AND selected_candidate_id IS NULL AND resolved_by IS NOT NULL AND resolved_at IS NOT NULL)
  );
