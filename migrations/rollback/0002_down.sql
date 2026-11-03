-- 回滚 0002：移除向量列与索引
DROP INDEX IF EXISTS idx_chunks_vector;

ALTER TABLE knowledge_chunks DROP CONSTRAINT IF EXISTS chk_embedding_model_present;
ALTER TABLE knowledge_chunks DROP COLUMN IF EXISTS embedding;

-- 扩展通常由实例级管理，不在此处 DROP EXTENSION；
-- 若确认实例上无其他使用者，可手工执行：DROP EXTENSION IF EXISTS vector;
