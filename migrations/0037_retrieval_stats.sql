-- 0037：检索统计子系统（精确 corpus/term 统计 + 版本失效）
--
-- 目标：把 lexical-v3 每次查询的全范围 N/avg_len/df 统计移到可重建的统计表，
-- 为 10 万切片性能优化提供基础；**本迁移不改变现有检索默认路径**，
-- 统计表未 READY 或 built_revision != source_revision 时，调用方回退 live。
--
-- 一致性边界（L1 2026-11-09）：
--   * 统计分组按 (tenant, scope, internal, acl_policy_id)；ACL 求值仍在请求侧按主体做，
--     统计表只存主体无关的分组聚合，不能替代 ACL；
--   * 所有影响可见集合的写路径通过触发器递增 source_revision（同一事务只递增一次）；
--   * 刷新按 tenant 串行化，新 generation 完整替换、单一 built_revision 原子发布；
--   * 读侧只有 READY 且 built_revision = source_revision 才使用统计表。

CREATE TABLE IF NOT EXISTS retrieval_stats_state (
  tenant_id        uuid PRIMARY KEY REFERENCES tenants(id) ON DELETE CASCADE,
  source_revision  bigint NOT NULL DEFAULT 0 CHECK (source_revision >= 0),
  built_revision   bigint CHECK (built_revision IS NULL OR built_revision >= 0),
  generation       bigint NOT NULL DEFAULT 0 CHECK (generation >= 0),
  status           text NOT NULL DEFAULT 'EMPTY'
                     CHECK (status IN ('EMPTY', 'READY', 'FAILED')),
  refreshed_at     timestamptz,
  last_error       text,
  CHECK (built_revision IS NULL OR built_revision <= source_revision),
  CHECK (status <> 'READY' OR built_revision IS NOT NULL)
);

CREATE TABLE IF NOT EXISTS retrieval_corpus_stats (
  tenant_id     uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  generation    bigint NOT NULL CHECK (generation >= 0),
  scope         text NOT NULL CHECK (scope IN (
                  'internal_product', 'internal_training', 'customer_public')),
  internal      boolean NOT NULL,
  acl_policy_id uuid NOT NULL,
  docs          bigint NOT NULL CHECK (docs > 0),
  total_len     bigint NOT NULL CHECK (total_len >= 0),
  PRIMARY KEY (tenant_id, generation, scope, internal, acl_policy_id),
  -- term 表用包含 docs 的复合 FK 强制 df <= 同一个 corpus.docs
  UNIQUE (tenant_id, generation, scope, internal, acl_policy_id, docs),
  FOREIGN KEY (tenant_id, acl_policy_id)
    REFERENCES acl_policies(tenant_id, id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS retrieval_term_stats (
  tenant_id     uuid NOT NULL,
  generation    bigint NOT NULL,
  scope         text NOT NULL,
  internal      boolean NOT NULL,
  acl_policy_id uuid NOT NULL,
  term          text NOT NULL CHECK (length(btrim(term)) > 0),
  df            bigint NOT NULL CHECK (df >= 0),
  docs          bigint NOT NULL CHECK (docs > 0),
  PRIMARY KEY (tenant_id, generation, scope, internal, acl_policy_id, term),
  FOREIGN KEY (tenant_id, generation, scope, internal, acl_policy_id, docs)
    REFERENCES retrieval_corpus_stats
      (tenant_id, generation, scope, internal, acl_policy_id, docs)
    ON DELETE CASCADE,
  CHECK (df <= docs)
);

CREATE INDEX IF NOT EXISTS idx_retrieval_term_lookup
  ON retrieval_term_stats (tenant_id, generation, scope, internal, term);

ALTER TABLE retrieval_stats_state ENABLE ROW LEVEL SECURITY;
ALTER TABLE retrieval_stats_state FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON retrieval_stats_state
  USING (tenant_id = fkh_current_tenant())
  WITH CHECK (tenant_id = fkh_current_tenant());

ALTER TABLE retrieval_corpus_stats ENABLE ROW LEVEL SECURITY;
ALTER TABLE retrieval_corpus_stats FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON retrieval_corpus_stats
  USING (tenant_id = fkh_current_tenant())
  WITH CHECK (tenant_id = fkh_current_tenant());

ALTER TABLE retrieval_term_stats ENABLE ROW LEVEL SECURITY;
ALTER TABLE retrieval_term_stats FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON retrieval_term_stats
  USING (tenant_id = fkh_current_tenant())
  WITH CHECK (tenant_id = fkh_current_tenant());

-- 已有租户先登记状态；built_revision=NULL 表示尚未构建，调用方必须回退 live。
INSERT INTO retrieval_stats_state (tenant_id)
SELECT id FROM tenants
ON CONFLICT (tenant_id) DO NOTHING;

-- 所有影响可见集合的写路径只递增一次（同一事务里 100k 个 chunk 不会产生 100k 次更新）。
-- SECURITY DEFINER：业务角色没有统计表的写权限，触发器代表迁移 owner 维护 revision。
CREATE OR REPLACE FUNCTION fkh_bump_retrieval_revision() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
  target_tenant uuid;
  marker text;
BEGIN
  IF TG_OP = 'DELETE' THEN
    target_tenant := OLD.tenant_id;
  ELSE
    target_tenant := NEW.tenant_id;
  END IF;
  -- custom GUC 名只能由点分隔的标识符组成，UUID 的连字符必须去掉
  marker := 'fkh.retrieval_bump.tx' || txid_current()::text || '.t'
            || replace(target_tenant::text, '-', '');
  IF current_setting(marker, true) = '1' THEN
    RETURN NULL;
  END IF;
  PERFORM set_config(marker, '1', true);
  INSERT INTO retrieval_stats_state (tenant_id, source_revision, status)
  VALUES (target_tenant, 1, 'EMPTY')
  ON CONFLICT (tenant_id) DO UPDATE
    SET source_revision = retrieval_stats_state.source_revision + 1;
  RETURN NULL;
END;
$$;

DROP TRIGGER IF EXISTS trg_retrieval_chunks_revision ON knowledge_chunks;
CREATE TRIGGER trg_retrieval_chunks_revision
  AFTER INSERT OR UPDATE OR DELETE ON knowledge_chunks
  FOR EACH ROW EXECUTE FUNCTION fkh_bump_retrieval_revision();

DROP TRIGGER IF EXISTS trg_retrieval_versions_revision ON knowledge_versions;
CREATE TRIGGER trg_retrieval_versions_revision
  AFTER INSERT OR UPDATE OR DELETE ON knowledge_versions
  FOR EACH ROW EXECUTE FUNCTION fkh_bump_retrieval_revision();

DROP TRIGGER IF EXISTS trg_retrieval_acl_revision ON acl_policies;
CREATE TRIGGER trg_retrieval_acl_revision
  AFTER INSERT OR UPDATE OR DELETE ON acl_policies
  FOR EACH ROW EXECUTE FUNCTION fkh_bump_retrieval_revision();

COMMENT ON TABLE retrieval_stats_state IS
  '检索统计构建状态：source_revision 由触发器递增；READY 且 built_revision=source_revision 才可读';
COMMENT ON TABLE retrieval_corpus_stats IS
  '主体无关的检索分组统计（tenant/scope/internal/ACL）；ACL 判定仍由请求侧按主体执行';
COMMENT ON TABLE retrieval_term_stats IS
  '检索分组内 term 的 document frequency；df <= docs；随 corpus 同 generation 原子替换';
