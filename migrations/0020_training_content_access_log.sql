-- 0020：培训正文读取审计（L1 2026-11-07 条件：成功返回正文的每页必须落一条）
--
-- 只记"谁、哪个任务、哪个课程修订、实际返回了哪些版本与块"，不记正文、
-- 不记令牌、不记原始 cursor。拒绝请求不写本表（拒绝原因留在访问日志/错误 trace）。

CREATE TABLE IF NOT EXISTS training_content_access_log (
  tenant_id       uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
  access_id       uuid NOT NULL DEFAULT gen_random_uuid(),
  principal_id    uuid NOT NULL,
  subject_id      text NOT NULL,
  assignment_id   uuid NOT NULL,
  course_id       uuid NOT NULL,
  course_version  integer NOT NULL,
  version_ids     uuid[] NOT NULL,
  chunk_ids       uuid[] NOT NULL,
  trace_id        text NOT NULL,
  occurred_at     timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, access_id)
);

ALTER TABLE training_content_access_log ENABLE ROW LEVEL SECURITY;
ALTER TABLE training_content_access_log FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON training_content_access_log
  USING (tenant_id = fkh_current_tenant())
  WITH CHECK (tenant_id = fkh_current_tenant());

CREATE INDEX IF NOT EXISTS idx_training_content_access_assignment
  ON training_content_access_log (tenant_id, assignment_id, occurred_at DESC);
