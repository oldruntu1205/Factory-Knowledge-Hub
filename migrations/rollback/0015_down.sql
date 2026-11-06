-- 回滚 0015：删除冲突复核队列。
--
-- 安全前提（2026-11-06 L1 裁决）：
--   - 任一新表存在数据（待办/候选/决议）即拒绝；
--   - 检查与删除在同一事务内，并先 LOCK 两张表，防止"检查通过后被并发写入"再丢数据；
--   - 两种情况下都**不得**改动 catalog_items。
DO $$
DECLARE
  group_count bigint;
  candidate_count bigint;
BEGIN
  LOCK TABLE catalog_conflict_groups, catalog_conflict_candidates IN EXCLUSIVE MODE;

  SELECT count(*) INTO group_count FROM catalog_conflict_groups;
  SELECT count(*) INTO candidate_count FROM catalog_conflict_candidates;
  IF group_count > 0 OR candidate_count > 0 THEN
    RAISE EXCEPTION
      'FKH_ROLLBACK_BLOCKED: 冲突队列仍有 % 个待办组、% 条候选，拒绝清表；'
      '请先导出/处置这些人工复核记录',
      group_count, candidate_count
      USING ERRCODE = '23514';
  END IF;
END;
$$;

-- 循环外键：先摘掉组表指向候选表的那个约束，否则两张表无法删除
ALTER TABLE catalog_conflict_groups DROP CONSTRAINT IF EXISTS fk_group_selected_candidate;

DROP POLICY IF EXISTS tenant_isolation ON catalog_conflict_candidates;
DROP POLICY IF EXISTS tenant_isolation ON catalog_conflict_groups;
DROP TABLE catalog_conflict_candidates;
DROP TABLE catalog_conflict_groups;
