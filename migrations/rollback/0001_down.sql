-- 回滚 0001：移除治理核心
DROP TRIGGER IF EXISTS trg_acl_touch ON acl_policies;
DROP TRIGGER IF EXISTS trg_chunks_touch ON knowledge_chunks;
DROP TRIGGER IF EXISTS trg_review_actions_no_delete ON review_actions;
DROP TRIGGER IF EXISTS trg_review_actions_no_update ON review_actions;
DROP TRIGGER IF EXISTS trg_version_no_delete ON knowledge_versions;
DROP TRIGGER IF EXISTS trg_version_guard ON knowledge_versions;
DROP TRIGGER IF EXISTS trg_chunk_parent_guard ON knowledge_chunks;

DROP FUNCTION IF EXISTS fkh_touch_updated_at();
DROP FUNCTION IF EXISTS fkh_block_delete();
DROP FUNCTION IF EXISTS fkh_guard_version_mutation();
DROP FUNCTION IF EXISTS fkh_assert_chunk_parent();

DROP TABLE IF EXISTS outbox_events;
DROP TABLE IF EXISTS bad_cases;
DROP TABLE IF EXISTS rag_citations;
DROP TABLE IF EXISTS rag_queries;
DROP TABLE IF EXISTS review_actions;
DROP TABLE IF EXISTS knowledge_chunks;
DROP TABLE IF EXISTS acl_policies;
DROP TABLE IF EXISTS source_assets;
DROP TABLE IF EXISTS knowledge_versions;
DROP TABLE IF EXISTS knowledge_documents;
DROP TABLE IF EXISTS principals;
DROP TABLE IF EXISTS tenants;
