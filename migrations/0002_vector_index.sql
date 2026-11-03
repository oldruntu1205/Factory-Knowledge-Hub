-- =============================================================================
-- Factory Knowledge Hub · 0002_vector_index.sql
-- 向量列与索引（独立成文件，使不含 pgvector 的环境仍可验证治理与 ACL 逻辑）
--
-- 依据：L1 §5.2 与 §0.3（BGE-M3 为候选，非最终决定）
-- 前置：0001_core_schema.sql
-- 回滚：rollback/0002_down.sql
-- =============================================================================

CREATE EXTENSION IF NOT EXISTS vector;

-- [补强-6] 维度绑定当前 Embedding 模型。更换模型时不得原地修改既有列：
--   1) 新增 embedding_v2 vector(N) 列与索引；2) 双写并灰度；3) 回填后切换；4) 删除旧列。
--   混存不同模型向量会直接破坏召回质量，且不可通过阈值补救。
ALTER TABLE knowledge_chunks
  ADD COLUMN IF NOT EXISTS embedding vector(1024);

COMMENT ON COLUMN knowledge_chunks.embedding IS 'BGE-M3 候选维度 1024d；embedding_model_id 必须与之一致，换模型走列迁移';

CREATE INDEX IF NOT EXISTS idx_chunks_vector
  ON knowledge_chunks USING hnsw (embedding vector_cosine_ops)
  WITH (m = 16, ef_construction = 200);

-- 向量一致性约束：已写向量的块必须声明模型 ID
ALTER TABLE knowledge_chunks
  DROP CONSTRAINT IF EXISTS chk_embedding_model_present;
ALTER TABLE knowledge_chunks
  ADD CONSTRAINT chk_embedding_model_present
  CHECK (embedding IS NULL OR embedding_model_id IS NOT NULL);
