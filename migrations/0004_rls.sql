-- =============================================================================
-- Factory Knowledge Hub · 0004_rls.sql
-- 行级安全：全表 ENABLE + FORCE ROW LEVEL SECURITY，按会话租户隔离
--
-- 依据：L1 §5.3、§13.4 强制迁移后门禁
-- 前置：0001–0003
-- 回滚：rollback/0004_down.sql
--
-- 设计说明与残余风险：
--   1. 应用层 Policy Engine 是第一道防线，RLS 是第二道。仅靠 RLS 不构成完整 ACL：
--      acl_policies 的文档级/行级规则仍由授权服务判定。
--   2. 策略仅比较租户。主体归属校验（principal 是否属于该租户、是否 active）
--      由应用层在认证上下文构建阶段完成。详见 docs/SPEC/threat-model.md 的“租户上下文伪造”条目。
--   3. app.tenant_id 与 app.principal_id 必须在同一事务内 SET LOCAL 设置，
--      且连接归还池前事务必须结束（见 src/fkh/authz/context.py）。
--   4. FORCE ROW LEVEL SECURITY 使表 owner 也受策略约束；仅超级用户与 BYPASSRLS 角色可绕过。
--      生产业务角色不得具备这两项属性。
-- =============================================================================

CREATE OR REPLACE FUNCTION fkh_current_tenant() RETURNS uuid
LANGUAGE sql STABLE PARALLEL SAFE AS $$
  SELECT NULLIF(current_setting('app.tenant_id', true), '')::uuid
$$;

CREATE OR REPLACE FUNCTION fkh_current_principal() RETURNS uuid
LANGUAGE sql STABLE PARALLEL SAFE AS $$
  SELECT NULLIF(current_setting('app.principal_id', true), '')::uuid
$$;

COMMENT ON FUNCTION fkh_current_tenant() IS
  '返回当前事务租户。未设置时返回 NULL，策略比较结果为 NULL，等价于拒绝（fail-closed）';

-- -----------------------------------------------------------------------------
-- tenants：以自身 id 作为隔离键
-- -----------------------------------------------------------------------------
ALTER TABLE tenants ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenants FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON tenants;
CREATE POLICY tenant_isolation ON tenants
  USING (id = fkh_current_tenant())
  WITH CHECK (id = fkh_current_tenant());

-- -----------------------------------------------------------------------------
-- 含 tenant_id 的业务表：统一策略
-- -----------------------------------------------------------------------------
DO $$
DECLARE
  target text;
  tables text[] := ARRAY[
    'principals',
    'knowledge_documents',
    'knowledge_versions',
    'source_assets',
    'acl_policies',
    'knowledge_chunks',
    'review_actions',
    'rag_queries',
    'rag_citations',
    'bad_cases',
    'outbox_events',
    'training_roles',
    'training_courses',
    'training_course_sources',
    'training_assignments',
    'training_questions',
    'training_attempts',
    'training_practical_signoffs'
  ];
BEGIN
  FOREACH target IN ARRAY tables LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', target);
    EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', target);
    EXECUTE format('DROP POLICY IF EXISTS tenant_isolation ON %I', target);
    EXECUTE format($fmt$
      CREATE POLICY tenant_isolation ON %I
        USING (tenant_id = fkh_current_tenant())
        WITH CHECK (tenant_id = fkh_current_tenant())
    $fmt$, target);
  END LOOP;
END;
$$;

-- -----------------------------------------------------------------------------
-- 自检：任何漏配 FORCE 的业务表都应被发现
-- -----------------------------------------------------------------------------
DO $$
DECLARE
  leaky text;
BEGIN
  SELECT string_agg(format('%I(rowsecurity=%s, forced=%s)', relname, relrowsecurity, relforcerowsecurity), ', ')
    INTO leaky
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
   WHERE n.nspname = current_schema()
     AND c.relkind = 'r'
     AND (c.relrowsecurity IS FALSE OR c.relforcerowsecurity IS FALSE)
     AND EXISTS (
       SELECT 1 FROM information_schema.columns col
        WHERE col.table_schema = current_schema()
          AND col.table_name = c.relname
          AND col.column_name IN ('tenant_id','id'));

  IF leaky IS NOT NULL THEN
    RAISE EXCEPTION 'FKH_RLS_INCOMPLETE: 以下表未启用强制 RLS: %', leaky;
  END IF;
END;
$$;
