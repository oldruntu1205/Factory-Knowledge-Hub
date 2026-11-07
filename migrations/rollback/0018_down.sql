-- 0018 回滚：从取值域里移除 handoff（若已存在该路径的历史记录则拒绝，避免破坏审计）
DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM rag_queries
     WHERE route = 'handoff' OR route_reason = 'external_customer_commitment'
  ) THEN
    RAISE EXCEPTION 'FKH_ROLLBACK_BLOCKED: 审计表中已有 handoff 记录，回滚会丢失路径语义';
  END IF;
END $$;

ALTER TABLE rag_queries DROP CONSTRAINT IF EXISTS rag_queries_route_check;
ALTER TABLE rag_queries ADD CONSTRAINT rag_queries_route_check
  CHECK (route IS NULL OR route IN ('rag', 'catalog_exact'));

ALTER TABLE rag_queries DROP CONSTRAINT IF EXISTS rag_queries_route_reason_check;
ALTER TABLE rag_queries ADD CONSTRAINT rag_queries_route_reason_check
  CHECK (route_reason IS NULL OR route_reason IN (
    'no_code', 'multi_code', 'code_without_intent',
    'accepted', 'not_found', 'ambiguous', 'inconsistent'));
