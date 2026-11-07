-- 0019：知识撤销/弃用 → 关联培训课程自动转 NEEDS_REVIEW
--
-- Rev2 §13 与 §1.1-7：知识撤销后，学员侧必须立即停止学习与问答，并在课程层面留痕。
-- 之前只有 `my_assignments`/`submit_attempt` 检查课程状态，但**没有任何路径把课程
-- 置为 NEEDS_REVIEW**——撤销知识后课程仍是 PUBLISHED，学员照样能学、能交卷。
--
-- 用数据库触发器而不是在 ReviewService 里写业务代码：撤销可能来自 API、回滚脚本、
-- 运维 SQL 三条路径，只有在数据层收口才能保证不漏。

CREATE OR REPLACE FUNCTION fkh_flag_training_on_version_retire() RETURNS trigger AS $$
BEGIN
  IF NEW.status IN ('REVOKED', 'DISCARDED')
     AND OLD.status IS DISTINCT FROM NEW.status THEN
    UPDATE training_courses c
       SET status = 'NEEDS_REVIEW', published_at = COALESCE(c.published_at, now())
     WHERE c.tenant_id = NEW.tenant_id
       AND c.status = 'PUBLISHED'
       AND EXISTS (
             SELECT 1 FROM training_course_sources s
              WHERE s.tenant_id = c.tenant_id
                AND s.course_id = c.course_id
                AND s.knowledge_version_id = NEW.id
           );
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_training_flag_on_version_retire ON knowledge_versions;
CREATE TRIGGER trg_training_flag_on_version_retire
  AFTER UPDATE OF status ON knowledge_versions
  FOR EACH ROW
  EXECUTE FUNCTION fkh_flag_training_on_version_retire();
