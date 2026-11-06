-- 回滚 0014：删除目录结构化表。
--
-- 安全前提（2026-11-06 L1 裁决）：**有数据则拒绝**。表里是从已入库文档抽出来的事实，
-- 静默清表等于丢证据；必须先确定这些条目是否还需要（或先备份）。
DO $$
DECLARE
  item_count bigint;
BEGIN
  SELECT count(*) INTO item_count FROM catalog_items;
  IF item_count > 0 THEN
    RAISE EXCEPTION
      'FKH_ROLLBACK_BLOCKED: catalog_items 仍有 % 条记录，拒绝自动清表；'
      '请先确认这些结构化事实可以丢弃（它们可由原文重新抽取）',
      item_count
      USING ERRCODE = '23514';
  END IF;
END;
$$;

DROP POLICY IF EXISTS tenant_isolation ON catalog_items;
DROP TABLE catalog_items;

ALTER TABLE rag_queries DROP CONSTRAINT IF EXISTS rag_queries_route_check;
ALTER TABLE rag_queries DROP CONSTRAINT IF EXISTS rag_queries_route_reason_check;
ALTER TABLE rag_queries DROP COLUMN IF EXISTS route_reason;
ALTER TABLE rag_queries DROP COLUMN IF EXISTS route;
