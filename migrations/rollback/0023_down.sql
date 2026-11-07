-- 0023 回滚：恢复 chunk_id 非空 + RESTRICT 外键（若已存在被置空的引用则拒绝）
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM rag_citations WHERE chunk_id IS NULL) THEN
    RAISE EXCEPTION 'FKH_ROLLBACK_BLOCKED: 已有引用指向已删除的块，回滚会破坏审计完整性';
  END IF;
END $$;

ALTER TABLE rag_citations DROP CONSTRAINT IF EXISTS rag_citations_tenant_id_chunk_id_fkey;
ALTER TABLE rag_citations
  ADD CONSTRAINT rag_citations_tenant_id_chunk_id_fkey
  FOREIGN KEY (tenant_id, chunk_id)
  REFERENCES knowledge_chunks (tenant_id, id)
  ON DELETE RESTRICT;
ALTER TABLE rag_citations ALTER COLUMN chunk_id SET NOT NULL;
