-- =============================================================================
-- Factory Knowledge Hub · 0008_retrieval_queries.sql
-- 检索审计：谁在什么范围检索、返回多少条、是否被拒
--
-- 依据：2026-11-03 L1 判决（_evidence/l1_reply_search_20261103.md）第 5 条
-- 前置：0000–0007
-- 回滚：rollback/0008_down.sql
--
-- 为什么不复用 rag_queries：
--   rag_queries 的 status 枚举是问答语义（ANSWERED/REFUSED/...），检索既没有回答也没有拒答；
--   把搜索塞进问答审计表会让"有多少次提问被拒答"这类统计从此失真。
--
-- 为什么 query_hash 要用服务端密钥做 HMAC：
--   普通 sha256 对短查询（如「价格」）可被字典反推，等于绕开"禁止落明文"的约束。
--   密钥不入表、不入日志；key_id 只用于轮换时识别用哪把密钥。
-- =============================================================================

CREATE TABLE retrieval_queries (
  id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id           uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
  principal_id        uuid NOT NULL,
  -- 服务端密钥的 HMAC，不落明文查询
  query_hash          text NOT NULL CHECK (query_hash ~ '^[0-9a-f]{64}$'),
  query_hash_key_id   text NOT NULL CHECK (length(btrim(query_hash_key_id)) > 0),
  knowledge_scope     text NOT NULL CHECK (knowledge_scope IN (
                        'customer_public', 'internal_training', 'internal_product')),
  -- 检索自己的状态枚举：成功（含空结果）、范围未授权、执行失败
  status              text NOT NULL CHECK (status IN ('SUCCEEDED', 'REFUSED', 'ERROR')),
  refusal_code        text,
  -- 无法确定返回数时留 NULL；正常空结果是 0，二者不可混用
  returned_count      integer CHECK (returned_count IS NULL OR returned_count >= 0),
  trace_id            text NOT NULL CHECK (length(btrim(trace_id)) > 0),
  duration_ms         integer NOT NULL CHECK (duration_ms >= 0),
  created_at          timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT ck_retrieval_refusal_code
    CHECK (
      (status = 'SUCCEEDED' AND refusal_code IS NULL)
      OR (status <> 'SUCCEEDED' AND refusal_code IS NOT NULL)
    ),
  UNIQUE (tenant_id, id)
);

COMMENT ON TABLE retrieval_queries IS
  '检索审计：不存 query 明文、片段、候选数或"全部被过滤"标志；审计读取权限须单独授予';
COMMENT ON COLUMN retrieval_queries.query_hash IS
  '服务端密钥的 HMAC-SHA256；密钥不入表，key_id 仅用于轮换识别';

CREATE INDEX idx_retrieval_queries_tenant_time
  ON retrieval_queries (tenant_id, created_at DESC);

ALTER TABLE retrieval_queries ENABLE ROW LEVEL SECURITY;
ALTER TABLE retrieval_queries FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON retrieval_queries;
CREATE POLICY tenant_isolation ON retrieval_queries
  USING (tenant_id = fkh_current_tenant())
  WITH CHECK (tenant_id = fkh_current_tenant());

-- 审计追加式：结论有误时新增一条，不改历史
CREATE OR REPLACE FUNCTION fkh_retrieval_queries_append_only() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'FKH_AUDIT_APPEND_ONLY: retrieval_queries 是追加式审计表，不允许 % (id=%)',
    TG_OP, COALESCE(OLD.id::text, '')
    USING ERRCODE = '23514';
END;
$$;

CREATE TRIGGER trg_retrieval_queries_no_update
  BEFORE UPDATE ON retrieval_queries
  FOR EACH ROW EXECUTE FUNCTION fkh_retrieval_queries_append_only();

CREATE TRIGGER trg_retrieval_queries_no_delete
  BEFORE DELETE ON retrieval_queries
  FOR EACH ROW EXECUTE FUNCTION fkh_retrieval_queries_append_only();

-- 自检：新表同样必须处于强制 RLS 之下
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
     AND c.relname = 'retrieval_queries'
     AND (c.relrowsecurity IS FALSE OR c.relforcerowsecurity IS FALSE);

  IF leaky IS NOT NULL THEN
    RAISE EXCEPTION 'FKH_RLS_INCOMPLETE: 以下表未启用强制 RLS: %', leaky;
  END IF;
END;
$$;
