-- 0019 回滚：移除触发器与函数（只影响"撤销→课程待复核"的联动，不改数据）
DROP TRIGGER IF EXISTS trg_training_flag_on_version_retire ON knowledge_versions;
DROP FUNCTION IF EXISTS fkh_flag_training_on_version_retire();
