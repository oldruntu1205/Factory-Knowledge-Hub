-- =============================================================================
-- Factory Knowledge Hub · 0005_content_uniqueness.sql
-- 同一文档下内容哈希唯一
--
-- 背景：ingest_document 在应用层用 find_version_by_content 做重复检查，
-- 但两个并发上传可以同时通过检查再各自插入，产生两份内容完全相同的版本。
-- 应用层检查只能改善错误信息，唯一性必须由数据库保证。
--
-- 前置：0001_core_schema.sql
-- 回滚：rollback/0005_down.sql
-- =============================================================================

CREATE UNIQUE INDEX IF NOT EXISTS uq_versions_document_content
  ON knowledge_versions (tenant_id, document_id, content_sha256);

COMMENT ON INDEX uq_versions_document_content IS
  '同一文档不允许出现内容哈希相同的两个版本；重复上传应在应用层返回既有版本，而不是新建';
