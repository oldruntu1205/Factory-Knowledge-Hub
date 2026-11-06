-- =============================================================================
-- Factory Knowledge Hub · 0014_catalog_items.sql
-- 目录结构化通道：把「货号 → 规格/单价」抽成可校验事实
--
-- 依据：2026-11-06 L1 裁决（_evidence/l1_reply_catalog_exact_20261106.md）
--   - 只对 domain='product_catalog' 抽取；
--   - 明确单一价格才入表；区间价/千分位/缺价不猜，保留原文并告警；
--   - 版本状态在查询时校验，不级联删除；
--   - 单价 numeric(12,2) + 原文行 raw_text + 原始价格串 raw_price；
--   - 同一版本内货号唯一；跨版本冲突由查询层判定（不得任取一条）。
-- 前置：0000–0013
-- 回滚：rollback/0014_down.sql（**有数据则拒绝**，不自动清表）
-- =============================================================================

CREATE TABLE catalog_items (
  item_id     uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id   uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
  version_id  uuid NOT NULL,
  chunk_id    uuid NOT NULL,
  code        text NOT NULL CHECK (code ~ '^[A-Z]{1,3}[0-9]{5,8}$'),
  spec        text NOT NULL CHECK (length(spec) > 0),
  -- 原文行：对账与引用定位以它为准
  raw_text    text NOT NULL,
  -- 原始价格串（保留千分位等写法），结构化只接受明确单一价格
  raw_price   text NOT NULL,
  price       numeric(12,2) NOT NULL CHECK (price >= 0),
  -- 货币必须来自明确的目录约定/元数据，这里只接受显式写入，不给默认值
  currency    text NOT NULL CHECK (currency IN ('CNY')),
  locator     jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at  timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, item_id),
  -- 同一版本内货号唯一：跨版本重复由查询层判歧义，不在这里合并
  UNIQUE (tenant_id, version_id, code),
  FOREIGN KEY (tenant_id, version_id)
    REFERENCES knowledge_versions(tenant_id, id) ON DELETE RESTRICT,
  FOREIGN KEY (tenant_id, chunk_id)
    REFERENCES knowledge_chunks(tenant_id, id) ON DELETE RESTRICT
);

CREATE INDEX idx_catalog_items_code ON catalog_items (tenant_id, code);

-- ---------------------------------------------------------------------------
-- 问答审计补「路由」维度：精确通道与 RAG 必须可区分
--   route_reason 只存短枚举，**不得**写入原始查询（查询仍只存 HMAC）
-- ---------------------------------------------------------------------------
ALTER TABLE rag_queries ADD COLUMN IF NOT EXISTS route text
  CHECK (route IS NULL OR route IN ('rag', 'catalog_exact'));
ALTER TABLE rag_queries ADD COLUMN IF NOT EXISTS route_reason text
  CHECK (route_reason IS NULL OR route_reason IN (
    'no_code', 'multi_code', 'code_without_intent',
    'accepted', 'not_found', 'ambiguous', 'inconsistent'));

COMMENT ON COLUMN rag_queries.route IS
  'rag = 检索增强生成；catalog_exact = 目录精确通道（确定性，不经过模型）';

COMMENT ON TABLE catalog_items IS
  '目录结构化事实：由 product_catalog 文档的「货号 规格 单价」行抽取；不改变原文与切片';
COMMENT ON COLUMN catalog_items.raw_text IS '原文整行，引用定位与对账以它为准';
COMMENT ON COLUMN catalog_items.raw_price IS '原始价格串；结构化 price 只接受明确单一价格';

-- ---------------------------------------------------------------------------
-- RLS：与既有业务表一致（ENABLE + FORCE + tenant_isolation）
-- ---------------------------------------------------------------------------
ALTER TABLE catalog_items ENABLE ROW LEVEL SECURITY;
ALTER TABLE catalog_items FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON catalog_items;
CREATE POLICY tenant_isolation ON catalog_items
  USING (tenant_id = fkh_current_tenant())
  WITH CHECK (tenant_id = fkh_current_tenant());

-- 自检：任何漏配 FORCE 的表都应在迁移期暴露，而不是等到线上
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_class
     WHERE relname = 'catalog_items' AND relrowsecurity AND relforcerowsecurity
  ) THEN
    RAISE EXCEPTION 'FKH_RLS_MISSING: catalog_items 未启用 FORCE ROW LEVEL SECURITY';
  END IF;
END;
$$;
