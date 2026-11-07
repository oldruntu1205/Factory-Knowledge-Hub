-- 0024 回滚：恢复为整行置空语义（会撞 tenant_id NOT NULL，故仅在无 SET NULL 需求时使用）
ALTER TABLE rag_citations DROP CONSTRAINT IF EXISTS rag_citations_tenant_id_chunk_id_fkey;
ALTER TABLE rag_citations
  ADD CONSTRAINT rag_citations_tenant_id_chunk_id_fkey
  FOREIGN KEY (tenant_id, chunk_id)
  REFERENCES knowledge_chunks (tenant_id, id)
  ON DELETE SET NULL;
