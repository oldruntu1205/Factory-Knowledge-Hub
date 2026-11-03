-- =============================================================================
-- Factory Knowledge Hub · 0000_extensions.sql
-- 扩展与基础配置
--
-- 依据：L1 架构契约 Rev.2 第 5 章
--
-- pgcrypto 仅用于兼容 PG < 13 的 gen_random_uuid()；PG 16 已内置该函数。
-- 因此按「可用则装、不可用则跳过」处理，避免精简实例（内嵌 PG、最小化镜像）
-- 因缺少扩展而无法建立治理表。向量扩展由 0002 单独声明。
-- =============================================================================

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = 'pgcrypto') THEN
    CREATE EXTENSION IF NOT EXISTS pgcrypto;
    RAISE NOTICE 'pgcrypto 已就绪';
  ELSE
    RAISE NOTICE 'pgcrypto 不可用；PG 13+ 内置 gen_random_uuid()，继续执行';
  END IF;
END;
$$;

DO $$
BEGIN
  IF current_setting('server_version_num')::int < 130000 THEN
    RAISE EXCEPTION 'FKH_PG_VERSION: 需要 PostgreSQL 13 及以上（当前 %）', current_setting('server_version')
      USING ERRCODE = '0A000';
  END IF;
END;
$$;
