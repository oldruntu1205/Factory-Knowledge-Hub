-- 0033：放宽 KEEP 的状态自洽约束，允许"候选已被清理"的 KEEP 组
--
-- 背景：0032 让 `selected_candidate_id` 在候选被清理时置空，但 0015 的 CHECK 要求
-- KEEP 必须 `selected_candidate_id IS NOT NULL` → 清理时撞 CHECK
-- （`catalog_conflict_groups_check`）。
--
-- 新口径：KEEP 必须"有候选 **或** 有决议快照"——候选被撤销清理后，决议事实由
-- `resolved_snapshot` 承载（0031），审计仍完整。

ALTER TABLE catalog_conflict_groups
  DROP CONSTRAINT IF EXISTS catalog_conflict_groups_check;

ALTER TABLE catalog_conflict_groups
  ADD CONSTRAINT catalog_conflict_groups_check CHECK (
    (status = 'OPEN'
       AND selected_candidate_id IS NULL
       AND resolved_by IS NULL
       AND resolved_at IS NULL
       AND resolved_snapshot IS NULL)
    OR (status = 'KEEP'
       AND resolved_by IS NOT NULL
       AND resolved_at IS NOT NULL
       AND (selected_candidate_id IS NOT NULL OR resolved_snapshot IS NOT NULL))
    OR (status = 'DROP_ALL'
       AND selected_candidate_id IS NULL
       AND resolved_by IS NOT NULL
       AND resolved_at IS NOT NULL)
  );
