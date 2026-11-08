-- 0032 回滚：恢复 0015 的 RESTRICT 语义（会重新阻止清理；仅在前瞻回退时使用）
ALTER TABLE catalog_conflict_groups DROP CONSTRAINT IF EXISTS fk_group_selected_candidate;
ALTER TABLE catalog_conflict_groups
  ADD CONSTRAINT fk_group_selected_candidate
  FOREIGN KEY (tenant_id, selected_candidate_id)
  REFERENCES catalog_conflict_candidates (tenant_id, candidate_id)
  ON DELETE RESTRICT;
