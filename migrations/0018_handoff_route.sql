-- 0018：问答审计支持"外部客服商务承诺转人工"这条路径
--
-- 背景（2026-11-07 对客 P0 纠偏）：Rev2 §4.5 要求外部客服的价格/交期/质量承诺/定制可行性
-- 一律转人工。这条路径既不是 RAG，也不是目录精确通道，但审计必须能如实记录，
-- 否则事后无法区分"模型拒答"与"策略转人工"。
--
-- 只放宽 CHECK 的取值域，不改列类型、不重写历史行（历史 route 值仍合法）。
ALTER TABLE rag_queries DROP CONSTRAINT IF EXISTS rag_queries_route_check;
ALTER TABLE rag_queries ADD CONSTRAINT rag_queries_route_check
  CHECK (route IS NULL OR route IN ('rag', 'catalog_exact', 'handoff'));

ALTER TABLE rag_queries DROP CONSTRAINT IF EXISTS rag_queries_route_reason_check;
ALTER TABLE rag_queries ADD CONSTRAINT rag_queries_route_reason_check
  CHECK (route_reason IS NULL OR route_reason IN (
    'no_code', 'multi_code', 'code_without_intent',
    'accepted', 'not_found', 'ambiguous', 'inconsistent',
    'external_customer_commitment'));

COMMENT ON COLUMN rag_queries.route IS
  'rag = 检索增强生成；catalog_exact = 目录精确通道（确定性，不经过模型）；
   handoff = 策略转人工（如外部客服商务承诺类问题，未进入检索/生成）';
