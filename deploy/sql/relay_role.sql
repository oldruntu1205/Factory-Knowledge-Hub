-- =============================================================================
-- Factory Knowledge Hub · deploy/sql/relay_role.sql
-- outbox 投递器（relay）的平台角色与最小授权
--
-- 依据：2026-11-03 L1 判决第 1 条（_evidence/l1_reply_outbox_20261103.md）
--   - 角色由平台管理身份创建，不写进迁移（角色是集群级对象，迁移无法回滚干净）；
--   - BYPASSRLS 是角色属性，不能靠「给业务角色加策略」模拟；
--   - 只授 SELECT + 投递状态列的列级 UPDATE，不授改租户、事件身份、封套内容的权限。
--
-- 幂等：可重复执行。生产上以 DBA/平台身份执行一次，随后由 relay 进程用该角色登录。
-- 凭据不得与业务角色共享，也不得下发给普通业务账号。
-- =============================================================================

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fkh_relay') THEN
    CREATE ROLE fkh_relay LOGIN;
  END IF;
END
$$;

-- 每次执行都收敛属性，避免有人手工把角色提权成超级用户
ALTER ROLE fkh_relay
  LOGIN
  NOINHERIT
  NOSUPERUSER
  NOCREATEDB
  NOCREATEROLE
  NOREPLICATION
  BYPASSRLS;

DO $$
BEGIN
  EXECUTE format('GRANT CONNECT ON DATABASE %I TO fkh_relay', current_database());
END
$$;

GRANT USAGE ON SCHEMA public TO fkh_relay;

-- 跨租户扫描需要 SELECT；这是 relay 存在的前提，也是它唯一的读权限
GRANT SELECT ON outbox_events TO fkh_relay;

-- 只允许改「投递状态」，不允许改 tenant_id / event_type / payload / aggregate_* / occurred_at
GRANT UPDATE (
  published_at,
  retry_count,
  next_attempt_at,
  locked_until,
  delivery_token,
  last_error,
  dead_lettered_at
) ON outbox_events TO fkh_relay;

-- 死信重放要写审计。重放不是普通业务动作：调用方必须传真实 actor_id 与原因，
-- 业务角色不得被授予这张表的 INSERT。
GRANT INSERT ON outbox_replays TO fkh_relay;
