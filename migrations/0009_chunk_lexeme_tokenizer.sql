-- =============================================================================
-- Factory Knowledge Hub · 0009_chunk_lexeme_tokenizer.sql
-- 把「切片 tokenizer」与「词法分词器」拆成两列
--
-- 背景：knowledge_chunks.tokenizer_id 在 L1 §4.3 的语义是**切片时用于计 token 的
-- tokenizer 版本**（如 approx-cjk-v1）。而 lexeme 由另一套中文分词规则生成
-- （fkh.domain.lexeme，cjk-char-v1）。两者混用同一列会直接导致：
--   入库写 approx-cjk-v1、检索按 cjk-char-v1 过滤 → 候选恒为 0，检索永远无结果。
--   这个组合不会报错，只是静默返回空，是最难排查的一类故障。
--
-- 因此新增独立列记录 lexeme 的分词器版本，检索按该列过滤；tokenizer_id 保持原语义。
--
-- 前置：0001（knowledge_chunks）、0005
-- 回滚：rollback/0009_down.sql
-- =============================================================================

ALTER TABLE knowledge_chunks
  ADD COLUMN IF NOT EXISTS lexeme_tokenizer_id text;

COMMENT ON COLUMN knowledge_chunks.lexeme_tokenizer_id IS
  '生成 lexeme 所用中文分词器的版本标识；与 tokenizer_id（切片 token 计数版本）不是同一概念';

-- 回填：仓库历史中所有 lexeme 均由 cjk-char-v1 生成（fkh.domain.lexeme）
UPDATE knowledge_chunks
   SET lexeme_tokenizer_id = 'cjk-char-v1'
 WHERE lexeme IS NOT NULL
   AND lexeme_tokenizer_id IS NULL;

-- 有 lexeme 必须有分词器版本，否则检索无法判断该行的倒排是否与当前规则兼容
ALTER TABLE knowledge_chunks
  DROP CONSTRAINT IF EXISTS chk_chunk_lexeme_tokenizer;
ALTER TABLE knowledge_chunks
  ADD CONSTRAINT chk_chunk_lexeme_tokenizer
  CHECK (lexeme IS NULL OR lexeme_tokenizer_id IS NOT NULL);

-- 检索按 (tenant, 分词器版本) 过滤后再走 GIN，避免跨版本混检
CREATE INDEX IF NOT EXISTS idx_chunks_lexeme_tokenizer
  ON knowledge_chunks (tenant_id, lexeme_tokenizer_id)
  WHERE lexeme_tokenizer_id IS NOT NULL;
