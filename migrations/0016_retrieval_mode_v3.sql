-- =============================================================================
-- Factory Knowledge Hub · 0016_retrieval_mode_v3.sql
-- 审计模式枚举补 `lexical-v3`（词法排序由 ts_rank_cd 改为 BM25 字级重排）
--
-- 依据：2026-11-06 L1 裁决（_evidence/l1_reply_lexical_bm25_20261106.md）
--   「同意新增前向迁移，追加允许 lexical-v3，保留 lexical-v2」。
-- 前置：0012（当前枚举 lexical-v1 / lexical-v2 / hybrid-rrf-v1 / not_executed）
-- 回滚：rollback/0016_down.sql（有 lexical-v3 存量数据则拒绝）
--
-- 为什么必须改枚举：排序函数换了，旧行记录的是当时的执行语义，不能回填、不能混同。
-- =============================================================================

ALTER TABLE retrieval_queries DROP CONSTRAINT IF EXISTS ck_retrieval_queries_mode;

ALTER TABLE retrieval_queries
  ADD CONSTRAINT ck_retrieval_queries_mode
  CHECK (retrieval_mode IN (
    'lexical-v1', 'lexical-v2', 'lexical-v3', 'hybrid-rrf-v1', 'not_executed'));

COMMENT ON COLUMN retrieval_queries.retrieval_mode IS
  '本次实际执行的召回模式：lexical-v1 字级 AND / lexical-v2 字级 OR + ts_rank_cd / '
  'lexical-v3 字级 OR + BM25 重排 / hybrid-rrf-v1 双路融合 / not_executed 召回前即被拒绝';

DO $$
DECLARE
  bad integer;
BEGIN
  SELECT count(*) INTO bad
    FROM retrieval_queries
   WHERE retrieval_mode NOT IN (
     'lexical-v1', 'lexical-v2', 'lexical-v3', 'hybrid-rrf-v1', 'not_executed');
  IF bad > 0 THEN
    RAISE EXCEPTION 'FKH_MODE_ENUM_INVALID: 有 % 行 retrieval_mode 不在枚举内', bad;
  END IF;
END;
$$;
