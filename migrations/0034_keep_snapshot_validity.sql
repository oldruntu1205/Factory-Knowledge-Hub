-- 0034：KEEP 必须有**结构有效**的决议快照（L1 2026-11-08 要求收敛项）
--
-- 0033 只要求 `resolved_snapshot IS NOT NULL`：`{}`、类型错误或缺字段的 JSON 也能通过，
-- 因此"数据库保证审计完整性"这句话站不住。本迁移加结构约束：
--   * 快照必须是 JSON 对象；
--   * 必须含 candidate_id / code / spec / raw_price / resolved_by / decision 六个键；
--   * 这些键的值必须是非空字符串（`decision` 固定为 'KEEP'）；
-- 用 coalesce 包住，避免 NULL 让 CHECK 判定为 unknown 而放行。

-- 先修复历史 KEEP：候选已被清理且当年没留快照的记录，补一个"内容不可恢复"的
-- 结构化占位（保留 code/操作者/备注，并标 legacy_recovered），而不是让迁移失败或编造内容。
UPDATE catalog_conflict_groups g
   SET resolved_snapshot = jsonb_build_object(
         'decision', 'KEEP',
         'candidate_id', coalesce(g.selected_candidate_id::text, 'legacy:' || g.group_id::text),
         'code', coalesce(g.code, ''),
         'spec', '(历史记录：内容不可恢复)',
         'raw_price', '(历史记录：内容不可恢复)',
         'resolved_by', coalesce(g.resolved_by::text, 'legacy:unknown'),
         'note', coalesce(g.note, ''),
         'legacy_recovered', true
       )
 WHERE g.status = 'KEEP'
   AND (
     g.resolved_snapshot IS NULL
     OR jsonb_typeof(g.resolved_snapshot) <> 'object'
     OR coalesce(g.resolved_snapshot ->> 'candidate_id', '') = ''
     OR coalesce(g.resolved_snapshot ->> 'spec', '') = ''
     OR coalesce(g.resolved_snapshot ->> 'raw_price', '') = ''
     OR coalesce(g.resolved_snapshot ->> 'resolved_by', '') = ''
     OR coalesce(g.resolved_snapshot ->> 'decision', '') <> 'KEEP'
   );

ALTER TABLE catalog_conflict_groups
  DROP CONSTRAINT IF EXISTS catalog_conflict_groups_keep_snapshot_check;

ALTER TABLE catalog_conflict_groups
  ADD CONSTRAINT catalog_conflict_groups_keep_snapshot_check CHECK (
    status <> 'KEEP'
    OR (
      resolved_snapshot IS NOT NULL
      AND jsonb_typeof(resolved_snapshot) = 'object'
      AND coalesce(resolved_snapshot ->> 'candidate_id', '') <> ''
      AND coalesce(resolved_snapshot ->> 'code', '') <> ''
      AND coalesce(resolved_snapshot ->> 'spec', '') <> ''
      AND coalesce(resolved_snapshot ->> 'raw_price', '') <> ''
      AND coalesce(resolved_snapshot ->> 'resolved_by', '') <> ''
      AND coalesce(resolved_snapshot ->> 'decision', '') = 'KEEP'
    )
  );

COMMENT ON CONSTRAINT catalog_conflict_groups_keep_snapshot_check ON catalog_conflict_groups IS
  'KEEP 决议必须带结构有效的快照（候选被撤销清理后的唯一审计凭据）';
