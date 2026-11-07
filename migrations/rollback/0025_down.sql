-- 0025 回滚：删除消费台账（不改知识块与事件）
DROP POLICY IF EXISTS tenant_isolation ON outbox_deliveries;
DROP TABLE IF EXISTS outbox_deliveries;
