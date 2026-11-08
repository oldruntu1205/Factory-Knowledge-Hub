-- 0032：修 0031 遗漏的循环外键——KEEP 决议的候选仍被 RESTRICT 挡住清理
--
-- 2026-11-08 复现：KEEP 决议 + 撤销 + 清理时，
--   update or delete on table "catalog_conflict_candidates" violates foreign key
--   constraint "fk_group_selected_candidate" ...
-- 原因是 0015 建的这个约束是 ON DELETE RESTRICT，而 0031 只新增了一个 SET NULL 约束，
-- 旧约束仍然生效。本迁移删掉两个重复约束，只保留一个"列级 SET NULL"的版本：
-- 候选被清理后 selected_candidate_id 置空，飞出的决议内容由 resolved_snapshot 保留。

ALTER TABLE catalog_conflict_groups
  DROP CONSTRAINT IF EXISTS fk_group_selected_candidate;
ALTER TABLE catalog_conflict_groups
  DROP CONSTRAINT IF EXISTS catalog_conflict_groups_selected_candidate_fkey;

ALTER TABLE catalog_conflict_groups
  ADD CONSTRAINT fk_group_selected_candidate
  FOREIGN KEY (tenant_id, selected_candidate_id)
  REFERENCES catalog_conflict_candidates (tenant_id, candidate_id)
  ON DELETE SET NULL (selected_candidate_id);
