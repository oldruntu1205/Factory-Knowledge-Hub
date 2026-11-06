-- =============================================================================
-- Factory Knowledge Hub · 0015_catalog_conflicts.sql
-- 目录冲突人工复核队列：待办组 + 候选行
--
-- 依据：2026-11-06 L1 裁决（_evidence/l1_reply_catalog_conflicts_20261106.md）
--   - 组表是"决议事实的唯一来源"，决议以组为单位，避免出现两个 KEEP / 部分已决议；
--   - 候选行保留完整来源（chunk + locator），原文相同可去重但来源不得丢；
--   - KEEP 用稳定的 candidate_id 引用，不用 raw_text/chunk_id 猜；
--   - 组状态：OPEN / KEEP / DROP_ALL，一次决议、不可原地修改；
--   - 版本撤销/弃用后保留记录、只读，不级联删除。
-- 前置：0000–0014
-- 回滚：rollback/0015_down.sql（**任一新表有数据即拒绝**，且不动 catalog_items）
-- =============================================================================

CREATE TABLE catalog_conflict_groups (
  group_id     uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id    uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
  version_id   uuid NOT NULL,
  code         text NOT NULL CHECK (code ~ '^[A-Z]{1,3}[0-9]{5,8}$'),
  status       text NOT NULL DEFAULT 'OPEN' CHECK (status IN ('OPEN', 'KEEP', 'DROP_ALL')),
  -- 决议选中的候选；OPEN 时必须为空
  selected_candidate_id uuid,
  resolved_by  uuid,
  resolved_at  timestamptz,
  note         text,
  created_at   timestamptz NOT NULL DEFAULT now(),
  updated_at   timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, group_id),
  -- 一个版本里同一货号只有一个待办组
  UNIQUE (tenant_id, version_id, code),
  FOREIGN KEY (tenant_id, version_id)
    REFERENCES knowledge_versions(tenant_id, id) ON DELETE RESTRICT,
  FOREIGN KEY (tenant_id, resolved_by)
    REFERENCES principals(tenant_id, id) ON DELETE RESTRICT,
  -- 状态与决议字段必须自洽：OPEN 无决议信息；KEEP 有选中且有人有时间；DROP_ALL 无人选但有人有时间
  CHECK (
    (status = 'OPEN'
       AND selected_candidate_id IS NULL AND resolved_by IS NULL AND resolved_at IS NULL)
    OR (status = 'KEEP'
       AND selected_candidate_id IS NOT NULL AND resolved_by IS NOT NULL AND resolved_at IS NOT NULL)
    OR (status = 'DROP_ALL'
       AND selected_candidate_id IS NULL AND resolved_by IS NOT NULL AND resolved_at IS NOT NULL)
  )
);

CREATE TABLE catalog_conflict_candidates (
  candidate_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id    uuid NOT NULL,
  group_id     uuid NOT NULL,
  chunk_id     uuid NOT NULL,
  raw_text     text NOT NULL,
  spec         text NOT NULL CHECK (length(spec) > 0),
  raw_price    text NOT NULL,
  price        numeric(12,2) NOT NULL CHECK (price >= 0),
  locator      jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at   timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, candidate_id),
  -- 同一组内同一来源同一原文只记一次（重抽取幂等）
  UNIQUE (tenant_id, group_id, chunk_id, raw_text),
  FOREIGN KEY (tenant_id, group_id)
    REFERENCES catalog_conflict_groups(tenant_id, group_id) ON DELETE RESTRICT,
  FOREIGN KEY (tenant_id, chunk_id)
    REFERENCES knowledge_chunks(tenant_id, id) ON DELETE RESTRICT
);

-- 选中候选必须属于同一组（循环外键，建表后再补）
ALTER TABLE catalog_conflict_groups
  ADD CONSTRAINT fk_group_selected_candidate
  FOREIGN KEY (tenant_id, selected_candidate_id)
  REFERENCES catalog_conflict_candidates(tenant_id, candidate_id)
  ON DELETE RESTRICT;

CREATE INDEX idx_conflict_groups_open
  ON catalog_conflict_groups (tenant_id, status);
CREATE INDEX idx_conflict_candidates_group
  ON catalog_conflict_candidates (tenant_id, group_id);

COMMENT ON TABLE catalog_conflict_groups IS
  '目录冲突待办组：一个版本内一个货号一条；决议（KEEP/DROP_ALL）以组为单位，一次决议不可原地修改';
COMMENT ON TABLE catalog_conflict_candidates IS
  '冲突候选原文行：保留 chunk 与 locator，KEEP 时按 candidate_id 回填 catalog_items';

-- ---------------------------------------------------------------------------
-- RLS：与既有业务表一致
-- ---------------------------------------------------------------------------
ALTER TABLE catalog_conflict_groups ENABLE ROW LEVEL SECURITY;
ALTER TABLE catalog_conflict_groups FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON catalog_conflict_groups;
CREATE POLICY tenant_isolation ON catalog_conflict_groups
  USING (tenant_id = fkh_current_tenant())
  WITH CHECK (tenant_id = fkh_current_tenant());

ALTER TABLE catalog_conflict_candidates ENABLE ROW LEVEL SECURITY;
ALTER TABLE catalog_conflict_candidates FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON catalog_conflict_candidates;
CREATE POLICY tenant_isolation ON catalog_conflict_candidates
  USING (tenant_id = fkh_current_tenant())
  WITH CHECK (tenant_id = fkh_current_tenant());

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_class
     WHERE relname = 'catalog_conflict_groups' AND relrowsecurity AND relforcerowsecurity
  ) OR NOT EXISTS (
    SELECT 1 FROM pg_class
     WHERE relname = 'catalog_conflict_candidates' AND relrowsecurity AND relforcerowsecurity
  ) THEN
    RAISE EXCEPTION 'FKH_RLS_MISSING: 冲突表未启用 FORCE ROW LEVEL SECURITY';
  END IF;
END;
$$;
