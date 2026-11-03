-- =============================================================================
-- Factory Knowledge Hub · rollback/0006_down.sql
-- 回滚顺序相反：先拆引用方（outbox_replays），再还原索引，最后删列与约束。
--
-- 执行前必须先停止并排空 relay（L1 判决）：
-- 运行中的 worker 仍在读写 locked_until / delivery_token，列被删掉后其确认语句会失败，
-- 事件会停在「已领取但无法确认」的状态，直到有人手工核对。
-- =============================================================================

DROP TRIGGER IF EXISTS trg_outbox_replays_no_update ON outbox_replays;
DROP TRIGGER IF EXISTS trg_outbox_replays_no_delete ON outbox_replays;
DROP FUNCTION IF EXISTS fkh_outbox_replays_append_only();
DROP TABLE IF EXISTS outbox_replays;

DROP INDEX IF EXISTS idx_outbox_aggregate_order;
DROP INDEX IF EXISTS idx_outbox_pending;

CREATE INDEX idx_outbox_pending ON outbox_events (occurred_at) WHERE published_at IS NULL;

ALTER TABLE outbox_events DROP CONSTRAINT IF EXISTS uq_outbox_aggregate_sequence;
ALTER TABLE outbox_events DROP CONSTRAINT IF EXISTS ck_outbox_terminal_without_lease;
ALTER TABLE outbox_events DROP CONSTRAINT IF EXISTS ck_outbox_terminal_exclusive;
ALTER TABLE outbox_events DROP CONSTRAINT IF EXISTS ck_outbox_last_error_length;
ALTER TABLE outbox_events DROP CONSTRAINT IF EXISTS ck_outbox_lease_pair;
ALTER TABLE outbox_events DROP CONSTRAINT IF EXISTS ck_outbox_event_ordinal;
ALTER TABLE outbox_events DROP CONSTRAINT IF EXISTS ck_outbox_trace_id_nonblank;
ALTER TABLE outbox_events DROP CONSTRAINT IF EXISTS uq_outbox_tenant_id;

ALTER TABLE outbox_events
  DROP COLUMN IF EXISTS dead_lettered_at,
  DROP COLUMN IF EXISTS last_error,
  DROP COLUMN IF EXISTS delivery_token,
  DROP COLUMN IF EXISTS locked_until,
  DROP COLUMN IF EXISTS next_attempt_at,
  DROP COLUMN IF EXISTS event_ordinal,
  DROP COLUMN IF EXISTS trace_id;
