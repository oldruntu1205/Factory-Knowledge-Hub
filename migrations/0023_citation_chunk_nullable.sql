-- 0023：撤销后物理清理让审计可追溯（rag_citations 不再阻止删除知识块）
--
-- 背景（2026-11-07 实测）：重建收敛需要删除已撤销版本的块，但 `rag_citations` 对
-- `knowledge_chunks` 有 RESTRICT 外键——历史引用会阻止删除，撤销内容永远留在库里。
-- 审计需要保留"哪次问答引用过哪个版本"，但不需要继续指向已删除的块本体。
--
-- 处理：chunk_id 允许为空，外键改为 ON DELETE SET NULL；保留 version_id 与 locator，
-- 审计仍可回答"引用过哪个版本的哪一段"，而块本体可以被真正删除（撤销内容不残留）。

ALTER TABLE rag_citations ALTER COLUMN chunk_id DROP NOT NULL;

ALTER TABLE rag_citations DROP CONSTRAINT IF EXISTS rag_citations_tenant_id_chunk_id_fkey;

ALTER TABLE rag_citations
  ADD CONSTRAINT rag_citations_tenant_id_chunk_id_fkey
  FOREIGN KEY (tenant_id, chunk_id)
  REFERENCES knowledge_chunks (tenant_id, id)
  ON DELETE SET NULL;

COMMENT ON COLUMN rag_citations.chunk_id IS
  '引用块 ID；块被撤销清理后置空（版本与 locator 仍保留，审计可追溯）';
