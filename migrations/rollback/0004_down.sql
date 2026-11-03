-- 回滚 0004：移除策略与 RLS 开关
DROP POLICY IF EXISTS tenant_isolation ON tenants;

DO $$
DECLARE
  target text;
  tables text[] := ARRAY[
    'principals','knowledge_documents','knowledge_versions','source_assets','acl_policies',
    'knowledge_chunks','review_actions','rag_queries','rag_citations','bad_cases','outbox_events',
    'training_roles','training_courses','training_course_sources','training_assignments',
    'training_questions','training_attempts','training_practical_signoffs'
  ];
BEGIN
  FOREACH target IN ARRAY tables LOOP
    IF to_regclass(target) IS NOT NULL THEN
      EXECUTE format('DROP POLICY IF EXISTS tenant_isolation ON %I', target);
      EXECUTE format('ALTER TABLE %I NO FORCE ROW LEVEL SECURITY', target);
      EXECUTE format('ALTER TABLE %I DISABLE ROW LEVEL SECURITY', target);
    END IF;
  END LOOP;
END;
$$;

ALTER TABLE tenants NO FORCE ROW LEVEL SECURITY;
ALTER TABLE tenants DISABLE ROW LEVEL SECURITY;

DROP FUNCTION IF EXISTS fkh_current_principal();
DROP FUNCTION IF EXISTS fkh_current_tenant();
