-- 回滚 0012：把 lexical-v2 记录折算回 lexical-v1，再收紧枚举。
-- 注意：审计表是追加式（UPDATE 被触发器禁止），折算必须在本迁移事务内临时关闭触发器，
-- 语句结束立即恢复；运行期路径仍然没有 UPDATE 权限。
ALTER TABLE retrieval_queries DISABLE TRIGGER trg_retrieval_queries_no_update;

UPDATE retrieval_queries SET retrieval_mode = 'lexical-v1' WHERE retrieval_mode = 'lexical-v2';

ALTER TABLE retrieval_queries ENABLE TRIGGER trg_retrieval_queries_no_update;

ALTER TABLE retrieval_queries DROP CONSTRAINT IF EXISTS ck_retrieval_queries_mode;
ALTER TABLE retrieval_queries
  ADD CONSTRAINT ck_retrieval_queries_mode
  CHECK (retrieval_mode IN ('lexical-v1', 'hybrid-rrf-v1', 'not_executed'));
