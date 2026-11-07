-- 0021：导师签核的可信授权（班组 / 岗位）
--
-- 2026-11-07 权限收口：签核此前只校验 `mentor` scope——任何持该 scope 的同租户主体
-- 都能签任何人的任务，也无法拒绝"学员自己签自己"。
-- 这里建立"导师 → 班组或岗位"的授权台账，签核时按任务的实际班组/岗位校验。

CREATE TABLE IF NOT EXISTS training_mentor_scopes (
  tenant_id         uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
  mentor_scope_id   uuid NOT NULL DEFAULT gen_random_uuid(),
  mentor_subject_id text NOT NULL,
  cohort_id         text,
  role_id           uuid,
  created_at        timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, mentor_scope_id),
  -- 一条授权要么覆盖班组、要么覆盖岗位，不能两者都空（否则等于"可签全部"）
  CONSTRAINT training_mentor_scope_target CHECK (
    (cohort_id IS NOT NULL AND role_id IS NULL)
    OR (cohort_id IS NULL AND role_id IS NOT NULL)
  )
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_training_mentor_scope
  ON training_mentor_scopes (
    tenant_id, mentor_subject_id,
    coalesce(cohort_id, ''), coalesce(role_id::text, '')
  );

ALTER TABLE training_mentor_scopes ENABLE ROW LEVEL SECURITY;
ALTER TABLE training_mentor_scopes FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON training_mentor_scopes
  USING (tenant_id = fkh_current_tenant())
  WITH CHECK (tenant_id = fkh_current_tenant());

COMMENT ON TABLE training_mentor_scopes IS
  '导师签核授权：按班组或岗位授予，签核时按任务实际归属校验；不得授予"全部"';
