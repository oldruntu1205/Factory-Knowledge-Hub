-- 回滚 0016：撤销 lexical-v3。
--
-- 按 2026-11-06 L1 裁决：
--   - 有 lexical-v3 存量数据则**拒绝**（那些行记录的是 BM25 语义，不能被改写成别的值）；
--   - 检查与收紧同一事务内完成，避免检查后又被写入的窗口。
DO $$
DECLARE
  used integer;
BEGIN
  SELECT count(*) INTO used FROM retrieval_queries WHERE retrieval_mode = 'lexical-v3';
  IF used > 0 THEN
    RAISE EXCEPTION
      'FKH_ROLLBACK_BLOCKED: 有 % 行记录使用 lexical-v3，拒绝收紧枚举；'
      '请先确认这些审计记录可以归档或保留在新约束之外',
      used
      USING ERRCODE = '23514';
  END IF;
END;
$$;

ALTER TABLE retrieval_queries DROP CONSTRAINT IF EXISTS ck_retrieval_queries_mode;
ALTER TABLE retrieval_queries
  ADD CONSTRAINT ck_retrieval_queries_mode
  CHECK (retrieval_mode IN ('lexical-v1', 'lexical-v2', 'hybrid-rrf-v1', 'not_executed'));
