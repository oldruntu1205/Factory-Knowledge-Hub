-- 0030：修 0022 回填的盲点——只回填了最大代，较早代永远不 READY
--
-- 2026-11-08 升级演练复现：旧库里存在多个索引代（1/2/3）。0022 的回填只写入了
-- `max(index_generation)` 这一代，于是停在较早代（1）的待发布版本升级后发布被拒（409），
-- 而"停在最大代"的版本恰好能过——这正是上一轮演练没暴露的盲点。
--
-- 修正：把每个租户**实际存在的每个代**都补成 READY（chunk_count 取该代真实行数），
-- 并把可见水位抬到最大代。已存在的行不改（0022 已写入的最大代保持原值）。

INSERT INTO index_generations (tenant_id, generation, status, chunk_count)
SELECT tenant_id, index_generation, 'READY', count(*)
  FROM knowledge_chunks
 GROUP BY tenant_id, index_generation
ON CONFLICT (tenant_id, generation) DO NOTHING;

INSERT INTO index_visibility (tenant_id, generation)
SELECT tenant_id, max(index_generation) FROM knowledge_chunks GROUP BY tenant_id
ON CONFLICT (tenant_id) DO UPDATE
  SET generation = GREATEST(index_visibility.generation, EXCLUDED.generation);
