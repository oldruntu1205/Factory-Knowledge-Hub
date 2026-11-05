-- 回滚 0013：撤销 DISCARDED 状态。
--
-- 安全前提（2026-11-05 L1 约束）：存在 DISCARDED 版本或 DISCARD 审计记录时，
-- **禁止**直接收紧约束，也**不得**自动改写成 REVOKED——那会把"从未公开"伪装成"曾公开后撤回"。
-- 这时必须由人工先决定这些数据怎么处置，再执行回滚。
DO $$
DECLARE
  discarded_versions integer;
  discard_actions integer;
BEGIN
  SELECT count(*) INTO discarded_versions FROM knowledge_versions WHERE status = 'DISCARDED';
  SELECT count(*) INTO discard_actions FROM review_actions WHERE action = 'DISCARD';
  IF discarded_versions > 0 OR discard_actions > 0 THEN
    RAISE EXCEPTION
      'FKH_ROLLBACK_BLOCKED: 存在 % 个 DISCARDED 版本、% 条 DISCARD 审计，'
      '不得自动收紧约束或改写为 REVOKED；请先人工处置这些数据',
      discarded_versions, discard_actions
      USING ERRCODE = '23514';
  END IF;
END;
$$;

-- 恢复培训联动：只对 REVOKED 触发
CREATE OR REPLACE FUNCTION fkh_course_needs_review_on_revoke() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.status = 'REVOKED' AND OLD.status IS DISTINCT FROM 'REVOKED' THEN
    UPDATE training_courses c
       SET status = 'NEEDS_REVIEW'
     WHERE c.tenant_id = NEW.tenant_id
       AND c.status = 'PUBLISHED'
       AND EXISTS (
         SELECT 1 FROM training_course_sources cs
          WHERE cs.tenant_id = c.tenant_id
            AND cs.course_id = c.course_id
            AND cs.knowledge_version_id = NEW.id);
  END IF;
  RETURN NULL;
END;
$$;

ALTER TABLE review_actions DROP CONSTRAINT IF EXISTS review_actions_from_status_check;
ALTER TABLE review_actions DROP COLUMN IF EXISTS from_status;

ALTER TABLE review_actions DROP CONSTRAINT IF EXISTS review_actions_action_check;
ALTER TABLE review_actions
  ADD CONSTRAINT review_actions_action_check
  CHECK (action IN (
    'SUBMIT','REVIEW_PASS','REVIEW_REJECT','APPROVE_INTERNAL',
    'APPROVE_PUBLIC','PUBLISH','SUPERSEDE','REVOKE'));

-- 恢复迁移白名单（去掉 DISCARDED 边）
CREATE OR REPLACE FUNCTION fkh_guard_version_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
  allowed boolean := false;
BEGIN
  IF NEW.document_id       IS DISTINCT FROM OLD.document_id
     OR NEW.version_no     IS DISTINCT FROM OLD.version_no
     OR NEW.title          IS DISTINCT FROM OLD.title
     OR NEW.classification IS DISTINCT FROM OLD.classification
     OR NEW.audience       IS DISTINCT FROM OLD.audience
     OR NEW.content_sha256 IS DISTINCT FROM OLD.content_sha256
     OR NEW.source_object_key IS DISTINCT FROM OLD.source_object_key
     OR NEW.created_at     IS DISTINCT FROM OLD.created_at
     OR NEW.tenant_id      IS DISTINCT FROM OLD.tenant_id THEN
    RAISE EXCEPTION 'FKH_VERSION_IMMUTABLE: 已创建版本的内容与分类不可修改，请创建新版本 (version_id=%)', OLD.id
      USING ERRCODE = '23514';
  END IF;

  IF NEW.status IS DISTINCT FROM OLD.status THEN
    allowed := (OLD.status, NEW.status) IN (
      ('RAW','DRAFT'),
      ('DRAFT','REVIEWED'),
      ('REVIEWED','APPROVED_INTERNAL'),
      ('REVIEWED','DRAFT'),
      ('APPROVED_INTERNAL','DRAFT'),
      ('APPROVED_INTERNAL','PUBLISHED_PUBLIC'),
      ('APPROVED_INTERNAL','SUPERSEDED'),
      ('APPROVED_INTERNAL','REVOKED'),
      ('PUBLISHED_PUBLIC','SUPERSEDED'),
      ('PUBLISHED_PUBLIC','REVOKED'),
      ('SUPERSEDED','REVOKED')
    );
    IF NOT allowed THEN
      RAISE EXCEPTION 'FKH_VERSION_TRANSITION: 非法状态迁移 % -> % (version_id=%)', OLD.status, NEW.status, OLD.id
        USING ERRCODE = '23514';
    END IF;
  END IF;

  IF NEW.status = 'PUBLISHED_PUBLIC' THEN
    IF NEW.approved_by IS NULL OR NEW.public_approved_by IS NULL THEN
      RAISE EXCEPTION 'FKH_PUBLISH_NO_APPROVER: 公开审批必须由技术审批人与公开审批人双签 (version_id=%)', NEW.id
        USING ERRCODE = '23514';
    END IF;
    IF NEW.public_approved_by = NEW.approved_by THEN
      RAISE EXCEPTION 'FKH_PUBLISH_SAME_APPROVER: 公开审批人不得与技术审批人为同一主体 (version_id=%)', NEW.id
        USING ERRCODE = '23514';
    END IF;
  END IF;

  IF NEW.status IN ('RAW','DRAFT') AND (NEW.approved_by IS NOT NULL OR NEW.public_approved_by IS NOT NULL) THEN
    RAISE EXCEPTION 'FKH_APPROVER_PREMATURE: 状态 % 不得存在审批人记录 (version_id=%)', NEW.status, NEW.id
      USING ERRCODE = '23514';
  END IF;

  RETURN NEW;
END;
$$;

ALTER TABLE knowledge_versions DROP CONSTRAINT IF EXISTS knowledge_versions_status_check;
ALTER TABLE knowledge_versions
  ADD CONSTRAINT knowledge_versions_status_check
  CHECK (status IN (
    'RAW','DRAFT','REVIEWED','APPROVED_INTERNAL',
    'PUBLISHED_PUBLIC','SUPERSEDED','REVOKED'));
