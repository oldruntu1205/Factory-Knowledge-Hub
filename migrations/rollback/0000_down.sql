-- 回滚 0000：移除扩展
-- 注意：pgcrypto 可能被实例上其他库共享，DROP 前确认依赖。
DROP EXTENSION IF EXISTS pgcrypto;
