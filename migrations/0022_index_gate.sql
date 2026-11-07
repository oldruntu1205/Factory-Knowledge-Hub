-- 0022：索引发布闸门与可见代数（Rev2 §7.2）
--
-- 发布：先构建索引 generation 并校验，再原子切换"可见代"；未就绪时不得让新版本提前可见。
-- 撤销：状态同步生效（API 立即拒绝），物理清理异步；重建后必须收敛（撤销内容彻底消失）。
--
-- 这里用两个小表承载语义：
--   index_generations —— 每代是否 READY（构建+校验完成）
--   index_visibility  —— 租户当前"可见代"水位（检索只认 <= 水位的块）
-- 回填：现有数据视为 READY，可见代 = 现存最大代，保证升级后行为不变。

CREATE TABLE IF NOT EXISTS index_generations (
  tenant_id   uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
  generation  bigint NOT NULL CHECK (generation >= 1),
  status      text NOT NULL CHECK (status IN ('BUILDING', 'READY', 'FAILED')),
  chunk_count integer NOT NULL DEFAULT 0 CHECK (chunk_count >= 0),
  built_at    timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, generation)
);

CREATE TABLE IF NOT EXISTS index_visibility (
  tenant_id   uuid PRIMARY KEY REFERENCES tenants(id) ON DELETE RESTRICT,
  generation  bigint NOT NULL CHECK (generation >= 1),
  switched_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE index_generations ENABLE ROW LEVEL SECURITY;
ALTER TABLE index_generations FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON index_generations
  USING (tenant_id = fkh_current_tenant())
  WITH CHECK (tenant_id = fkh_current_tenant());

ALTER TABLE index_visibility ENABLE ROW LEVEL SECURITY;
ALTER TABLE index_visibility FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON index_visibility
  USING (tenant_id = fkh_current_tenant())
  WITH CHECK (tenant_id = fkh_current_tenant());

-- 回填：已有块视为已就绪并可见
INSERT INTO index_generations (tenant_id, generation, status, chunk_count)
SELECT tenant_id, max(index_generation), 'READY', count(*)
  FROM knowledge_chunks
 GROUP BY tenant_id
ON CONFLICT (tenant_id, generation) DO NOTHING;

INSERT INTO index_visibility (tenant_id, generation)
SELECT tenant_id, max(generation) FROM index_generations GROUP BY tenant_id
ON CONFLICT (tenant_id) DO NOTHING;
