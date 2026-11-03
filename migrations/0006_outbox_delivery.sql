-- =============================================================================
-- Factory Knowledge Hub · 0006_outbox_delivery.sql
-- outbox 投递状态：租约、令牌、重试预算、死信、业务顺序与重放审计
--
-- 依据：L1 §7.2（业务状态与 outbox 同一事务、至少一次投递、幂等去重、死信与重试）
--       + 2026-11-03 L1 判决（_evidence/l1_reply_outbox_20261103.md）
-- 前置：0001–0005
-- 回滚：rollback/0006_down.sql
--
-- 设计要点（与 L1 判决逐条对应）：
--   1. 领取用「租约 + 令牌」，不是「事务锁持有到投递完成」：
--      locked_until 表示租约到期时间，delivery_token 标识本次领取者。
--      确认/失败/续租都必须同时校验 id + delivery_token + 租约未过期，
--      更新零行即代表租约已失效，worker 必须停止写这条事件。
--   2. retry_count 语义 = 「已领取的投递尝试次数」，在领取时加一，失败时不再加。
--      这样 worker 领完就崩也能耗尽预算（第 8 次领取后崩溃 → 租约过期后被清理为死信）。
--   3. 投递顺序按 (aggregate_version, event_ordinal)，不按 occurred_at：
--      同一事务内 now() 恒定，UUID 是随机的，二者都不能表达业务顺序。
--      同一版本有多条事件时，必须由生产者显式写不同的 event_ordinal。
--   4. trace_id 允许 NULL：封套用 COALESCE(trace_id, id::text) 填充，
--      含义是「事件自身链路」，不是上游请求 trace（见 docs/SPEC/outbox-delivery.md）。
--   5. 死信不自动复活；人工重放必须留审计（outbox_replays，追加式）。
-- =============================================================================

-- -----------------------------------------------------------------------------
-- 1. 投递状态列
-- -----------------------------------------------------------------------------
ALTER TABLE outbox_events
  ADD COLUMN trace_id         text,
  ADD COLUMN event_ordinal    integer NOT NULL DEFAULT 0,
  ADD COLUMN next_attempt_at  timestamptz NOT NULL DEFAULT now(),
  ADD COLUMN locked_until     timestamptz,
  ADD COLUMN delivery_token   uuid,
  ADD COLUMN last_error       text,
  ADD COLUMN dead_lettered_at timestamptz,

  -- trace 要么没有，要么是有内容的字符串：空串会让封套出现 minLength 冲突
  ADD CONSTRAINT ck_outbox_trace_id_nonblank
    CHECK (trace_id IS NULL OR length(btrim(trace_id)) > 0),

  ADD CONSTRAINT ck_outbox_event_ordinal
    CHECK (event_ordinal >= 0),

  -- 租约与令牌必须同生同灭，避免出现「有租约没令牌」（无法校验持有者）的状态
  ADD CONSTRAINT ck_outbox_lease_pair
    CHECK (
      (locked_until IS NULL AND delivery_token IS NULL)
      OR (locked_until IS NOT NULL AND delivery_token IS NOT NULL)
    ),

  ADD CONSTRAINT ck_outbox_last_error_length
    CHECK (last_error IS NULL OR char_length(last_error) <= 500),

  -- 已发布与已死信互斥：两个终态同时出现说明清理流程有缺陷
  ADD CONSTRAINT ck_outbox_terminal_exclusive
    CHECK (published_at IS NULL OR dead_lettered_at IS NULL),

  -- 终态不得继续持有租约
  ADD CONSTRAINT ck_outbox_terminal_without_lease
    CHECK (
      (published_at IS NULL AND dead_lettered_at IS NULL)
      OR (locked_until IS NULL AND delivery_token IS NULL)
    ),

  -- 业务顺序唯一键：同一聚合版本内 ordinal 不得重复。
  -- 与 0001 的 (…, event_type) 唯一键并存：那一条防「同一事件重复写入」，
  -- 这一条防「同一版本两条事件抢同一个位置」。
  ADD CONSTRAINT uq_outbox_aggregate_sequence
    UNIQUE (tenant_id, aggregate_type, aggregate_id, aggregate_version, event_ordinal);

COMMENT ON COLUMN outbox_events.trace_id IS
  '事件产生时的上游 trace；NULL 时封套使用事件 id，不表示上游请求 trace';
COMMENT ON COLUMN outbox_events.event_ordinal IS
  '同一聚合版本内由生产者确定的事件顺序；单事件默认 0';
COMMENT ON COLUMN outbox_events.retry_count IS
  '已领取的投递尝试次数；领取时增加，包含领取后崩溃的尝试';
COMMENT ON COLUMN outbox_events.delivery_token IS
  '每次领取生成的新令牌；确认、失败及续租须校验令牌和有效租约';

-- -----------------------------------------------------------------------------
-- 2. 索引：待投递扫描 + 同聚合顺序判定
-- -----------------------------------------------------------------------------
DROP INDEX IF EXISTS idx_outbox_pending;

CREATE INDEX idx_outbox_pending
  ON outbox_events (next_attempt_at, occurred_at, id)
  WHERE published_at IS NULL AND dead_lettered_at IS NULL;

CREATE INDEX idx_outbox_aggregate_order
  ON outbox_events (
    tenant_id,
    aggregate_type,
    aggregate_id,
    aggregate_version,
    event_ordinal
  )
  WHERE published_at IS NULL;

-- -----------------------------------------------------------------------------
-- 3. 死信重放审计（追加式，不可改不可删）
-- -----------------------------------------------------------------------------

-- outbox_events 缺少 (tenant_id, id) 复合唯一键，重放表需要它作为复合外键的被引用键。
-- 必须先建键再建表，否则 FK 找不到唯一约束。
ALTER TABLE outbox_events ADD CONSTRAINT uq_outbox_tenant_id UNIQUE (tenant_id, id);

CREATE TABLE outbox_replays (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id   uuid NOT NULL,
  event_id    uuid NOT NULL,
  actor_id    uuid NOT NULL,
  reason      text NOT NULL CHECK (length(btrim(reason)) > 0),
  replayed_at timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (tenant_id, event_id) REFERENCES outbox_events(tenant_id, id) ON DELETE RESTRICT,
  FOREIGN KEY (tenant_id, actor_id) REFERENCES principals(tenant_id, id)     ON DELETE RESTRICT,
  UNIQUE (tenant_id, id)
);

CREATE INDEX idx_outbox_replays_event ON outbox_replays (tenant_id, event_id, replayed_at);

ALTER TABLE outbox_replays ENABLE ROW LEVEL SECURITY;
ALTER TABLE outbox_replays FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON outbox_replays;
CREATE POLICY tenant_isolation ON outbox_replays
  USING (tenant_id = fkh_current_tenant())
  WITH CHECK (tenant_id = fkh_current_tenant());

CREATE OR REPLACE FUNCTION fkh_outbox_replays_append_only() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'FKH_AUDIT_APPEND_ONLY: outbox_replays 是追加式审计表，不允许 % (id=%)',
    TG_OP, COALESCE(OLD.id::text, '')
    USING ERRCODE = '23514';
END;
$$;

CREATE TRIGGER trg_outbox_replays_no_update
  BEFORE UPDATE ON outbox_replays
  FOR EACH ROW EXECUTE FUNCTION fkh_outbox_replays_append_only();

CREATE TRIGGER trg_outbox_replays_no_delete
  BEFORE DELETE ON outbox_replays
  FOR EACH ROW EXECUTE FUNCTION fkh_outbox_replays_append_only();

-- -----------------------------------------------------------------------------
-- 4. 自检：新增表也必须处于强制 RLS 之下（与 0004 的自检同口径）
-- -----------------------------------------------------------------------------
DO $$
DECLARE
  leaky text;
BEGIN
  SELECT string_agg(relname, ', ')
    INTO leaky
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
   WHERE n.nspname = current_schema()
     AND c.relkind = 'r'
     AND c.relname = 'outbox_replays'
     AND (c.relrowsecurity IS FALSE OR c.relforcerowsecurity IS FALSE);

  IF leaky IS NOT NULL THEN
    RAISE EXCEPTION 'FKH_RLS_INCOMPLETE: 以下表未启用强制 RLS: %', leaky;
  END IF;
END;
$$;
