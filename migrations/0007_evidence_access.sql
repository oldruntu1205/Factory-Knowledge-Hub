-- =============================================================================
-- Factory Knowledge Hub · 0007_evidence_access.sql
-- 证据定位的前置：版本级必需 scope + 访问审计
--
-- 依据：2026-11-03 L1 判决（_evidence/l1_reply_evidence_20261103.md）第 4、6 条
-- 前置：0000–0006
-- 回滚：rollback/0007_down.sql
--
-- 为什么需要 required_scope：
--   `audience='internal'` 不足以判定一条证据该给「内部产品」还是「内部培训」的人看。
--   从 audience 反推 scope 会让培训材料被产品侧账号读到（反之亦然）。
--   因此新增显式列，且**不设默认值**：未赋值的版本在证据定位上一律拒绝，
--   存量数据必须由业务逐条确认，不能靠 `internal_product` 兜底。
--
-- 为什么访问审计放在本切片：
--   证据定位是"按 ID 直接取正文"的入口，绕过检索，越权一旦发生没有别的地方留痕。
--   拒绝记录也要落库，因此 evidence_id 不设外键（目标可能根本不存在）。
-- =============================================================================

-- -----------------------------------------------------------------------------
-- 1. 版本级必需 scope
-- -----------------------------------------------------------------------------
ALTER TABLE knowledge_versions
  ADD COLUMN required_scope text;

ALTER TABLE knowledge_versions
  ADD CONSTRAINT ck_versions_required_scope
  CHECK (
    required_scope IS NULL
    OR (
      audience = 'internal'
      AND required_scope IN ('internal_product', 'internal_training')
    )
    OR (
      audience = 'customer_public'
      AND required_scope = 'customer_public'
    )
  );

COMMENT ON COLUMN knowledge_versions.required_scope IS
  '读取该版本所需的知识范围（internal_product / internal_training / customer_public）；'
  'NULL 表示尚未由业务确认，任何证据定位请求一律拒绝，不设默认值';

-- -----------------------------------------------------------------------------
-- 2. 证据访问审计
-- -----------------------------------------------------------------------------
CREATE TABLE evidence_access_log (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id        uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
  -- 不设 principals 外键：身份来自 IdP 令牌，可能尚未同步到本地主体表；
  -- 为了"能审计"而要求主体先存在，会让未同步用户在审计失败时被放行或被迫 500。
  principal_id     uuid NOT NULL,
  evidence_id      uuid NOT NULL,
  -- 实际提供正文的块：返回 child 时等于 evidence_id，恢复 parent 时是 parent id
  text_evidence_id uuid,
  result           text NOT NULL CHECK (result IN ('ALLOWED', 'DENIED')),
  -- 安全可判定的拒绝原因；对外一律 403，不暴露这一列
  reason           text NOT NULL CHECK (reason IN (
                     'AUTHORIZED',
                     'RESOURCE_UNAVAILABLE',
                     'STATE_NOT_VISIBLE',
                     'SCOPE_NOT_ALLOWED',
                     'ACL_DENIED',
                     'PARENT_INCOMPLETE')),
  trace_id         text NOT NULL CHECK (length(btrim(trace_id)) > 0),
  occurred_at      timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, id)
);

COMMENT ON TABLE evidence_access_log IS
  '证据定位访问审计：不含正文、签名链接或存储路径。拒绝记录不要求 evidence 存在';

CREATE INDEX idx_evidence_access_tenant_time
  ON evidence_access_log (tenant_id, occurred_at DESC);

ALTER TABLE evidence_access_log ENABLE ROW LEVEL SECURITY;
ALTER TABLE evidence_access_log FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON evidence_access_log;
CREATE POLICY tenant_isolation ON evidence_access_log
  USING (tenant_id = fkh_current_tenant())
  WITH CHECK (tenant_id = fkh_current_tenant());

-- 审计追加式：可更正结论的方式是新增一条，而不是改写历史
CREATE OR REPLACE FUNCTION fkh_evidence_access_append_only() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'FKH_AUDIT_APPEND_ONLY: evidence_access_log 是追加式审计表，不允许 % (id=%)',
    TG_OP, COALESCE(OLD.id::text, '')
    USING ERRCODE = '23514';
END;
$$;

CREATE TRIGGER trg_evidence_access_no_update
  BEFORE UPDATE ON evidence_access_log
  FOR EACH ROW EXECUTE FUNCTION fkh_evidence_access_append_only();

CREATE TRIGGER trg_evidence_access_no_delete
  BEFORE DELETE ON evidence_access_log
  FOR EACH ROW EXECUTE FUNCTION fkh_evidence_access_append_only();

-- -----------------------------------------------------------------------------
-- 3. 自检：新表同样必须处于强制 RLS 之下
-- -----------------------------------------------------------------------------
DO $$
DECLARE
  leaky text;
BEGIN
  SELECT string_agg(relname, ', ')
    INTO leaky
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
   WHERE n.nspname = current_schema()
     AND c.relkind = 'r'
     AND c.relname = 'evidence_access_log'
     AND (c.relrowsecurity IS FALSE OR c.relforcerowsecurity IS FALSE);

  IF leaky IS NOT NULL THEN
    RAISE EXCEPTION 'FKH_RLS_INCOMPLETE: 以下表未启用强制 RLS: %', leaky;
  END IF;
END;
$$;
