-- =============================================================================
-- Factory Knowledge Hub · 0013_discarded_status.sql
-- 知识版本终止路径：新增 DISCARDED（从未公开发布、主动作废）
--
-- 依据：2026-11-05 L1 裁决 B（_evidence/l1_reply_discarded_status_20261105.md）
--   - 允许 RAW / DRAFT / REVIEWED / APPROVED_INTERNAL → DISCARDED
--   - PUBLISHED_PUBLIC / SUPERSEDED / REVOKED 不得弃用（曾对外可见或已终止，须走 REVOKED）
--   - DISCARDED 为终态；版本号不回收
--   - 弃用写独立动作 DISCARD、独立事件 knowledge.version.discarded.v1
--   - 引用被弃用版本的已发布课程同样转 NEEDS_REVIEW
-- 前置：0000–0012（0010 预留给草稿终止但按「禁止倒序回填」不得补号，本切片取 0013）
-- 回滚：rollback/0013_down.sql（存在 DISCARDED 数据时拒绝收紧约束）
-- =============================================================================

-- ---------------------------------------------------------------------------
-- 1) 版本状态枚举
-- ---------------------------------------------------------------------------
ALTER TABLE knowledge_versions DROP CONSTRAINT IF EXISTS knowledge_versions_status_check;
ALTER TABLE knowledge_versions
  ADD CONSTRAINT knowledge_versions_status_check
  CHECK (status IN (
    'RAW','DRAFT','REVIEWED','APPROVED_INTERNAL',
    'PUBLISHED_PUBLIC','SUPERSEDED','REVOKED','DISCARDED'));

-- ---------------------------------------------------------------------------
-- 2) 迁移白名单：新增四条弃用边
--    其余规则（内容不可变、双签、审批人前置）原样保留。
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION fkh_guard_version_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
  allowed boolean := false;
BEGIN
  -- 1) 内容不可变
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

  -- 2) 状态迁移白名单（§3.2 + 2026-11-05 弃用裁决）
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
      ('SUPERSEDED','REVOKED'),
      -- 弃用：只允许"从未对外发布"的状态进入 DISCARDED
      ('RAW','DISCARDED'),
      ('DRAFT','DISCARDED'),
      ('REVIEWED','DISCARDED'),
      ('APPROVED_INTERNAL','DISCARDED')
    );
    IF NOT allowed THEN
      RAISE EXCEPTION 'FKH_VERSION_TRANSITION: 非法状态迁移 % -> % (version_id=%)', OLD.status, NEW.status, OLD.id
        USING ERRCODE = '23514';
    END IF;
  END IF;

  -- 3) 进入 PUBLISHED_PUBLIC 时审批人必须齐全且相互独立
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

  -- 4) 未进入 APPROVED_INTERNAL 及之后状态，不得残留审批人
  --    注意：DISCARDED 可能来自 APPROVED_INTERNAL，此时审批人是历史事实，必须保留（裁决 Q4）
  IF NEW.status IN ('RAW','DRAFT') AND (NEW.approved_by IS NOT NULL OR NEW.public_approved_by IS NOT NULL) THEN
    RAISE EXCEPTION 'FKH_APPROVER_PREMATURE: 状态 % 不得存在审批人记录 (version_id=%)', NEW.status, NEW.id
      USING ERRCODE = '23514';
  END IF;

  RETURN NEW;
END;
$$;

-- ---------------------------------------------------------------------------
-- 3) 审核动作：新增 DISCARD，并记录来源状态
-- ---------------------------------------------------------------------------
ALTER TABLE review_actions DROP CONSTRAINT IF EXISTS review_actions_action_check;
ALTER TABLE review_actions
  ADD CONSTRAINT review_actions_action_check
  CHECK (action IN (
    'SUBMIT','REVIEW_PASS','REVIEW_REJECT','APPROVE_INTERNAL',
    'APPROVE_PUBLIC','PUBLISH','SUPERSEDE','REVOKE','DISCARD'));

-- 来源状态：弃用审计必须能回答"从哪个状态作废的"（裁决 Q1）
-- 历史行留 NULL（当时未记录），不用猜测回填。
ALTER TABLE review_actions ADD COLUMN IF NOT EXISTS from_status text;
ALTER TABLE review_actions DROP CONSTRAINT IF EXISTS review_actions_from_status_check;
ALTER TABLE review_actions
  ADD CONSTRAINT review_actions_from_status_check
  CHECK (from_status IS NULL OR from_status IN (
    'RAW','DRAFT','REVIEWED','APPROVED_INTERNAL',
    'PUBLISHED_PUBLIC','SUPERSEDED','REVOKED','DISCARDED'));

COMMENT ON COLUMN review_actions.from_status IS
  '动作发生前的版本状态；2026-11-05 起写入（弃用审计必需），历史行留 NULL';

-- ---------------------------------------------------------------------------
-- 4) 培训联动：弃用与撤销同等待遇
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION fkh_course_needs_review_on_revoke() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.status IN ('REVOKED', 'DISCARDED') AND NEW.status IS DISTINCT FROM OLD.status THEN
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

-- 自检：触发器白名单必须真的包含 DISCARDED（防止 CREATE OR REPLACE 没生效就"以为改好了"）
DO $$
DECLARE
  fn text;
BEGIN
  SELECT pg_get_functiondef(oid) INTO fn
    FROM pg_proc WHERE proname = 'fkh_guard_version_mutation';
  IF fn IS NULL OR position('DISCARDED' in fn) = 0 THEN
    RAISE EXCEPTION 'FKH_DISCARD_WIRING_INCOMPLETE: 触发器白名单未包含 DISCARDED';
  END IF;
END;
$$;
