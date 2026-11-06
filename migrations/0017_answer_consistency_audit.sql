-- =============================================================================
-- Factory Knowledge Hub · 0017_answer_consistency_audit.sql
-- 问答审计补「答案—证据一致性检查」的结构化字段
--
-- 依据：2026-11-06 L1 裁决（_evidence/l1_reply_consistency_20261106.md）
--   「至少记录检查规则版本、两类 claim 的总数与支持数、命中原因、检查耗时；
--     某类没有 claim 时记为"不适用"，不能记成 100%」。
-- 前置：0014（route/route_reason）
-- 回滚：rollback/0017_down.sql（有数据则拒绝，避免抹掉复核依据）
--
-- 只加列，不加表；原因码 UNSUPPORTED_CLAIM 写在既有 refusal_code（无枚举约束）。
-- "不适用"用 NULL 表达：该列有值才代表这类 claim 真的被检查过。
-- =============================================================================

ALTER TABLE rag_queries
  ADD COLUMN IF NOT EXISTS consistency_rule   text,
  ADD COLUMN IF NOT EXISTS codes_total        integer,
  ADD COLUMN IF NOT EXISTS codes_supported    integer,
  ADD COLUMN IF NOT EXISTS numbers_total      integer,
  ADD COLUMN IF NOT EXISTS numbers_supported  integer,
  ADD COLUMN IF NOT EXISTS consistency_ms     integer;

ALTER TABLE rag_queries DROP CONSTRAINT IF EXISTS ck_rag_consistency_counts;
ALTER TABLE rag_queries
  ADD CONSTRAINT ck_rag_consistency_counts
  CHECK (
    (codes_total IS NULL OR codes_total >= 0)
    AND (codes_supported IS NULL OR codes_supported >= 0)
    AND (numbers_total IS NULL OR numbers_total >= 0)
    AND (numbers_supported IS NULL OR numbers_supported >= 0)
    AND (consistency_ms IS NULL OR consistency_ms >= 0)
    AND (codes_supported IS NULL OR codes_total IS NOT NULL)
    AND (numbers_supported IS NULL OR numbers_total IS NOT NULL)
    AND (codes_supported IS NULL OR codes_supported <= codes_total)
    AND (numbers_supported IS NULL OR numbers_supported <= numbers_total)
  );

COMMENT ON COLUMN rag_queries.consistency_rule IS
  '答案—证据一致性检查的规则版本（如 literal-support-v1）；NULL 表示本次未做检查';
COMMENT ON COLUMN rag_queries.codes_total IS '答案里抽出的货号数量；NULL=本次没有货号类 claim（不适用）';
COMMENT ON COLUMN rag_queries.codes_supported IS '其中在被引用证据里字面出现的数量';
COMMENT ON COLUMN rag_queries.numbers_total IS '答案里抽出的带单位数值数量；NULL=不适用';
COMMENT ON COLUMN rag_queries.numbers_supported IS '其中字面获得支持的数量';
COMMENT ON COLUMN rag_queries.consistency_ms IS '检查耗时（毫秒）';
