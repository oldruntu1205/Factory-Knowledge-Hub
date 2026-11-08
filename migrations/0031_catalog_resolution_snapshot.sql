-- 0031：目录决议的审计快照（撤销清理后仍可追溯）
--
-- 背景（2026-11-08）：0029 让冲突候选随块级联删除（撤销收敛必需），但 KEEP 决议
-- 只靠 `selected_candidate_id` 指向候选行——候选被清理后，人工决议就只剩一个悬空 UUID，
-- 无法回答"当时保留的是哪一行、什么规格、什么价格、由谁定的"。
--
-- 处理：
--   1) 决议时把候选内容快照进 `resolved_snapshot`（规格/原文/价格/定位/候选 ID/操作者）；
--   2) `selected_candidate_id` 加外键 ON DELETE SET NULL：候选被清理后置空，但快照仍在。

ALTER TABLE catalog_conflict_groups
  ADD COLUMN IF NOT EXISTS resolved_snapshot jsonb;

-- 回填：已有 KEEP 决议按当前候选补快照（升级即生效）
UPDATE catalog_conflict_groups g
   SET resolved_snapshot = jsonb_build_object(
         'candidate_id', g.selected_candidate_id,
         'code', g.code,
         'raw_text', c.raw_text,
         'spec', c.spec,
         'raw_price', c.raw_price,
         'price', c.price,
         'locator', c.locator,
         'resolved_by', g.resolved_by,
         'resolved_at', g.resolved_at,
         'note', g.note
       )
  FROM catalog_conflict_candidates c
 WHERE g.selected_candidate_id IS NOT NULL
   AND c.tenant_id = g.tenant_id
   AND c.candidate_id = g.selected_candidate_id
   AND g.resolved_snapshot IS NULL;

ALTER TABLE catalog_conflict_groups
  DROP CONSTRAINT IF EXISTS catalog_conflict_groups_selected_candidate_fkey;

ALTER TABLE catalog_conflict_groups
  ADD CONSTRAINT catalog_conflict_groups_selected_candidate_fkey
  FOREIGN KEY (tenant_id, selected_candidate_id)
  REFERENCES catalog_conflict_candidates (tenant_id, candidate_id)
  ON DELETE SET NULL (selected_candidate_id);

COMMENT ON COLUMN catalog_conflict_groups.resolved_snapshot IS
  '人工决议快照（候选内容 + 操作者 + 时间）；候选被撤销清理后仍可审计';
