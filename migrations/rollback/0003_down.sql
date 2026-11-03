-- 回滚 0003：移除培训增量
DROP VIEW IF EXISTS v_training_questions_learner;

DROP TRIGGER IF EXISTS trg_signoff_guard ON training_practical_signoffs;
DROP TRIGGER IF EXISTS trg_course_needs_review ON knowledge_versions;
DROP TRIGGER IF EXISTS trg_course_publish_guard ON training_courses;

DROP FUNCTION IF EXISTS fkh_guard_signoff();
DROP FUNCTION IF EXISTS fkh_course_needs_review_on_revoke();
DROP FUNCTION IF EXISTS fkh_guard_course_publish();

DROP TABLE IF EXISTS training_practical_signoffs;
DROP TABLE IF EXISTS training_attempts;
DROP TABLE IF EXISTS training_questions;
DROP TABLE IF EXISTS training_assignments;
DROP TABLE IF EXISTS training_course_sources;
DROP TABLE IF EXISTS training_courses;
DROP TABLE IF EXISTS training_roles;
