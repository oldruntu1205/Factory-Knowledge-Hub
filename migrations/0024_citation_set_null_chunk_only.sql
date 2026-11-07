-- 0024：复合外键的置空只作用于 chunk_id
--
-- 0023 把 `rag_citations` 的外键改成 ON DELETE SET NULL，但它是复合外键
-- (tenant_id, chunk_id)：PG 默认会把两列都置空，tenant_id 有 NOT NULL → 删除块时报错
-- （2026-11-07 实测）。PG15+ 允许指定列：ON DELETE SET NULL (chunk_id)。

ALTER TABLE rag_citations DROP CONSTRAINT IF EXISTS rag_citations_tenant_id_chunk_id_fkey;

ALTER TABLE rag_citations
  ADD CONSTRAINT rag_citations_tenant_id_chunk_id_fkey
  FOREIGN KEY (tenant_id, chunk_id)
  REFERENCES knowledge_chunks (tenant_id, id)
  ON DELETE SET NULL (chunk_id);
