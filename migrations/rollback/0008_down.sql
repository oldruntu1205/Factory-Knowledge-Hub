-- =============================================================================
-- Factory Knowledge Hub · rollback/0008_down.sql
-- 注意：回滚会删除检索审计。若需要保留，先导出：
--   \copy (SELECT * FROM retrieval_queries) TO 'retrieval_queries.csv' CSV HEADER
-- =============================================================================

DROP TRIGGER IF EXISTS trg_retrieval_queries_no_update ON retrieval_queries;
DROP TRIGGER IF EXISTS trg_retrieval_queries_no_delete ON retrieval_queries;
DROP FUNCTION IF EXISTS fkh_retrieval_queries_append_only();
DROP TABLE IF EXISTS retrieval_queries;
