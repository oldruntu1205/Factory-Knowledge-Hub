-- 回滚 0011：移除召回模式列（历史回填值随列一起丢弃，审计的其他字段不受影响）
ALTER TABLE retrieval_queries DROP CONSTRAINT IF EXISTS ck_retrieval_queries_mode;
ALTER TABLE retrieval_queries DROP COLUMN IF EXISTS retrieval_mode;
