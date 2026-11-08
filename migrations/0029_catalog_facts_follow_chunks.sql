-- 0029：目录事实与冲突候选随块清理（撤销收敛）
--
-- 2026-11-08 复现：撤销一个带目录条目和冲突候选的版本后，清理被外键挡住——
--   catalog_items_tenant_id_chunk_id_fkey
--   catalog_conflict_candidates_tenant_id_chunk_id_fkey
-- 都是 ON DELETE RESTRICT，于是"撤销=API 拒绝但数据永久留库"，与 §7.2 的收敛要求冲突。
--
-- 处理：这两类行是从块**派生**出来的事实（块没了它们就失去依据），改为级联删除。
-- 版本本身的 FK 保持 RESTRICT（版本是追加式记录，本就不允许删除）。

ALTER TABLE catalog_items
  DROP CONSTRAINT IF EXISTS catalog_items_tenant_id_chunk_id_fkey;
ALTER TABLE catalog_items
  ADD CONSTRAINT catalog_items_tenant_id_chunk_id_fkey
  FOREIGN KEY (tenant_id, chunk_id)
  REFERENCES knowledge_chunks (tenant_id, id)
  ON DELETE CASCADE;

ALTER TABLE catalog_conflict_candidates
  DROP CONSTRAINT IF EXISTS catalog_conflict_candidates_tenant_id_chunk_id_fkey;
ALTER TABLE catalog_conflict_candidates
  ADD CONSTRAINT catalog_conflict_candidates_tenant_id_chunk_id_fkey
  FOREIGN KEY (tenant_id, chunk_id)
  REFERENCES knowledge_chunks (tenant_id, id)
  ON DELETE CASCADE;

COMMENT ON TABLE catalog_items IS
  '目录结构化事实（由块抽取）；块被撤销清理时级联删除，避免撤销后事实残留';
