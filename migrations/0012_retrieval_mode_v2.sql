-- =============================================================================
-- Factory Knowledge Hub · 0012_retrieval_mode_v2.sql
-- 审计模式枚举补 `lexical-v2`（词法查询构造由逐字 AND 改为字级 OR）
--
-- 依据：2026-11-04 L1 裁决 A（_evidence/l1_reply_lexical_and_fix_20261104.md）
--   「采用 lexical-v2」——查询组合从 AND 变成 OR 会实质改变候选集合，
--   审计与响应都必须能区分新旧语义，即使排序函数未变。
-- 前置：0011（retrieval_mode 列与 CHECK）
-- 回滚：rollback/0012_down.sql
--
-- 为什么不能改 0011：已应用的迁移被 apply.py 做校验和比对，改动即漂移。
-- 新增取值只能通过新迁移扩展枚举，历史行保持原值（v1 记录的是当时的执行语义）。
-- =============================================================================

ALTER TABLE retrieval_queries DROP CONSTRAINT IF EXISTS ck_retrieval_queries_mode;

ALTER TABLE retrieval_queries
  ADD CONSTRAINT ck_retrieval_queries_mode
  CHECK (retrieval_mode IN ('lexical-v1', 'lexical-v2', 'hybrid-rrf-v1', 'not_executed'));

COMMENT ON COLUMN retrieval_queries.retrieval_mode IS
  '本次实际执行的召回模式：lexical-v1 字级 AND（2026-11-04 前）/ lexical-v2 字级 OR / hybrid-rrf-v1 双路融合 / not_executed 召回前即被拒绝';

-- 自检：枚举内不得出现未知取值
DO $$
DECLARE
  bad integer;
BEGIN
  SELECT count(*) INTO bad
    FROM retrieval_queries
   WHERE retrieval_mode NOT IN ('lexical-v1', 'lexical-v2', 'hybrid-rrf-v1', 'not_executed');
  IF bad > 0 THEN
    RAISE EXCEPTION 'FKH_MODE_ENUM_INVALID: 有 % 行 retrieval_mode 不在枚举内', bad;
  END IF;
END;
$$;
