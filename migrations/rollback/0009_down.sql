-- 回滚 0009：移除词法分词器版本列
DROP INDEX IF EXISTS idx_chunks_lexeme_tokenizer;

ALTER TABLE knowledge_chunks DROP CONSTRAINT IF EXISTS chk_chunk_lexeme_tokenizer;
ALTER TABLE knowledge_chunks DROP COLUMN IF EXISTS lexeme_tokenizer_id;
