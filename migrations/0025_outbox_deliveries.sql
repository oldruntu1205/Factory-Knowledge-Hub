-- 0025：outbox 消费台账（本片只覆盖撤销清理，通用框架留 ADR-06）
--
-- L1 2026-11-08：撤销清理必须有持久消费、幂等、重试与死信/可追踪失败，
-- 不能只有"可人工调用的函数"。这里以 (tenant, event_id, consumer) 为幂等键。

CREATE TABLE IF NOT EXISTS outbox_deliveries (
  tenant_id    uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
  event_id     uuid NOT NULL,
  consumer     text NOT NULL,
  status       text NOT NULL CHECK (status IN ('PENDING', 'DONE', 'FAILED')),
  attempts     integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
  last_error   text,
  updated_at   timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, event_id, consumer),
  FOREIGN KEY (tenant_id, event_id) REFERENCES outbox_events(tenant_id, id) ON DELETE CASCADE
);

ALTER TABLE outbox_deliveries ENABLE ROW LEVEL SECURITY;
ALTER TABLE outbox_deliveries FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON outbox_deliveries
  USING (tenant_id = fkh_current_tenant())
  WITH CHECK (tenant_id = fkh_current_tenant());

CREATE INDEX IF NOT EXISTS idx_outbox_deliveries_status
  ON outbox_deliveries (tenant_id, consumer, status, updated_at);
