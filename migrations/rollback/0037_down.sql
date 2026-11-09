-- 0037 down：删除检索统计子系统
DROP TRIGGER IF EXISTS trg_retrieval_acl_revision ON acl_policies;
DROP TRIGGER IF EXISTS trg_retrieval_versions_revision ON knowledge_versions;
DROP TRIGGER IF EXISTS trg_retrieval_chunks_revision ON knowledge_chunks;
DROP FUNCTION IF EXISTS fkh_bump_retrieval_revision();
DROP TABLE IF EXISTS retrieval_term_stats;
DROP TABLE IF EXISTS retrieval_corpus_stats;
DROP TABLE IF EXISTS retrieval_stats_state;
