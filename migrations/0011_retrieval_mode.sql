-- =============================================================================
-- Factory Knowledge Hub · 0011_retrieval_mode.sql
-- 检索审计补「本次实际执行的召回模式」
--
-- 依据：2026-11-04 L1 判决 Q3（_evidence/l1_reply_hybrid_wiring_20261104.md）
--   模式属于「执行配置事实」，与候选数量、过滤结果所泄露的内容存在性不同，不冲突；
--   审计补齐不得推迟到混合模式启用之后。
-- 前置：0000–0009（0010 已登记给草稿终止方案，本迁移按 L1 裁决取 0011）
-- 回滚：rollback/0011_down.sql
--
-- 为什么历史回填分两种取值：
--   本迁移之前的检索链路**只可能是纯词法**（SearchService 只调用 LexicalRetriever），
--   但其中 REFUSED 的记录（范围未授权）在召回前就被拒绝，从未执行任何召回，
--   回填成 lexical-v1 会把「未执行」伪记成「执行过词法」。因此按有依据的粒度回填。
--
-- 为什么允许在迁移里临时关掉追加式触发器：
--   retrieval_queries 的 UPDATE/DELETE 被触发器禁止（审计追加式）。
--   回填历史行是唯一一次受控例外，只在本迁移事务内关闭，语句结束立即恢复；
--   运行期路径没有任何 UPDATE 权限。
-- =============================================================================

ALTER TABLE retrieval_queries
  ADD COLUMN IF NOT EXISTS retrieval_mode text;

ALTER TABLE retrieval_queries DISABLE TRIGGER trg_retrieval_queries_no_update;

-- 执行过词法召回的记录（成功与执行期失败）——有依据回填
UPDATE retrieval_queries
   SET retrieval_mode = 'lexical-v1'
 WHERE retrieval_mode IS NULL
   AND status <> 'REFUSED';

-- 召回前就被拒绝的记录——从未执行召回，不得记为执行过词法
UPDATE retrieval_queries
   SET retrieval_mode = 'not_executed'
 WHERE retrieval_mode IS NULL;

ALTER TABLE retrieval_queries ENABLE TRIGGER trg_retrieval_queries_no_update;

ALTER TABLE retrieval_queries ALTER COLUMN retrieval_mode SET NOT NULL;

-- 新写入必须显式给出模式：留默认值会让「忘了传」静默记成词法，
-- 将来接线错误会以「审计看起来正常」的方式藏起来。
ALTER TABLE retrieval_queries ALTER COLUMN retrieval_mode DROP DEFAULT;

ALTER TABLE retrieval_queries DROP CONSTRAINT IF EXISTS ck_retrieval_queries_mode;
ALTER TABLE retrieval_queries
  ADD CONSTRAINT ck_retrieval_queries_mode
  CHECK (retrieval_mode IN ('lexical-v1', 'hybrid-rrf-v1', 'not_executed'));

COMMENT ON COLUMN retrieval_queries.retrieval_mode IS
  '本次实际执行的召回模式：lexical-v1 纯词法 / hybrid-rrf-v1 双路融合 / not_executed 召回前即被拒绝；不含候选数与过滤统计';

-- 自检：回填后不得残留 NULL，也不得出现枚举外取值
DO $$
DECLARE
  missing integer;
BEGIN
  SELECT count(*) INTO missing FROM retrieval_queries WHERE retrieval_mode IS NULL;
  IF missing > 0 THEN
    RAISE EXCEPTION 'FKH_BACKFILL_INCOMPLETE: 仍有 % 行 retrieval_mode 为空', missing;
  END IF;
END;
$$;
