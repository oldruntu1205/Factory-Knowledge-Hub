-- =============================================================================
-- Factory Knowledge Hub · rollback/0007_down.sql
-- 回滚顺序：先拆审计表（含触发器与函数），再删版本列与约束。
--
-- 注意：删除 required_scope 会同时丢失"业务已确认的读取范围"。
-- 生产回滚前必须导出该列（SELECT tenant_id, id, required_scope FROM knowledge_versions），
-- 否则重新迁移后所有版本都会退回"未赋值⇒不可取"的状态。
-- =============================================================================

DROP TRIGGER IF EXISTS trg_evidence_access_no_update ON evidence_access_log;
DROP TRIGGER IF EXISTS trg_evidence_access_no_delete ON evidence_access_log;
DROP FUNCTION IF EXISTS fkh_evidence_access_append_only();
DROP TABLE IF EXISTS evidence_access_log;

ALTER TABLE knowledge_versions DROP CONSTRAINT IF EXISTS ck_versions_required_scope;
ALTER TABLE knowledge_versions DROP COLUMN IF EXISTS required_scope;
