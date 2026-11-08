-- 0029 回滚：恢复 RESTRICT（若已存在被级联删除的痕迹则无法恢复数据，只恢复约束语义）
ALTER TABLE catalog_conflict_candidates
  DROP CONSTRAINT IF EXISTS catalog_conflict_candidates_tenant_id_chunk_id_fkey;
ALTER TABLE catalog_conflict_candidates
  ADD CONSTRAINT catalog_conflict_candidates_tenant_id_chunk_id_fkey
  FOREIGN KEY (tenant_id, chunk_id)
  REFERENCES knowledge_chunks (tenant_id, id)
  ON DELETE RESTRICT;

ALTER TABLE catalog_items
  DROP CONSTRAINT IF EXISTS catalog_items_tenant_id_chunk_id_fkey;
ALTER TABLE catalog_items
  ADD CONSTRAINT catalog_items_tenant_id_chunk_id_fkey
  FOREIGN KEY (tenant_id, chunk_id)
  REFERENCES knowledge_chunks (tenant_id, id)
  ON DELETE RESTRICT;
