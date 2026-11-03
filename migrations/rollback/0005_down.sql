-- 回滚 0005：移除内容唯一约束
DROP INDEX IF EXISTS uq_versions_document_content;
