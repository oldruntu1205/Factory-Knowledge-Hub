-- =============================================================================
-- Factory Knowledge Hub · 0003_training.sql
-- 新人培训增量（D-06 已确认范围）
--
-- 依据：L1 §13.4 参考 DDL + §13.4 末尾「强制迁移后门禁」
-- 补强：补齐课程对知识版本的复合外键；课程发布门禁；知识撤销联动 NEEDS_REVIEW；
--       学员不得自行签核；幂等键防重复分配与重复交卷。
-- 前置：0001_core_schema.sql
-- 回滚：rollback/0003_down.sql
-- =============================================================================

CREATE TABLE training_roles (
  tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
  role_id   uuid NOT NULL,
  code      text NOT NULL,
  name      text NOT NULL,
  PRIMARY KEY (tenant_id, role_id),
  UNIQUE (tenant_id, code)
);

CREATE TABLE training_courses (
  tenant_id    uuid NOT NULL,
  course_id    uuid NOT NULL,
  role_id      uuid NOT NULL,
  title        text NOT NULL,
  status       text NOT NULL CHECK (status IN ('DRAFT','PUBLISHED','NEEDS_REVIEW','ARCHIVED')),
  version      integer NOT NULL CHECK (version > 0),
  published_at timestamptz,
  created_at   timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, course_id),
  FOREIGN KEY (tenant_id, role_id) REFERENCES training_roles(tenant_id, role_id) ON DELETE RESTRICT,
  -- 发布态必须有发布时间
  CHECK (status <> 'PUBLISHED' OR published_at IS NOT NULL)
);

CREATE TABLE training_course_sources (
  tenant_id           uuid NOT NULL,
  course_id           uuid NOT NULL,
  knowledge_version_id uuid NOT NULL,
  PRIMARY KEY (tenant_id, course_id, knowledge_version_id),
  FOREIGN KEY (tenant_id, course_id) REFERENCES training_courses(tenant_id, course_id) ON DELETE CASCADE,
  -- [补强] §13.4 标注 L2 必须补齐的复合外键：课程只能引用同租户的知识版本
  FOREIGN KEY (tenant_id, knowledge_version_id)
    REFERENCES knowledge_versions(tenant_id, id) ON DELETE RESTRICT
);

CREATE TABLE training_assignments (
  tenant_id           uuid NOT NULL,
  assignment_id       uuid NOT NULL,
  course_id           uuid NOT NULL,
  learner_subject_id  text NOT NULL,
  cohort_id           text,
  status              text NOT NULL CHECK (status IN (
                        'ASSIGNED','IN_PROGRESS','PENDING_PRACTICAL','COMPLETED','REMEDIATION')),
  idempotency_key     text,
  assigned_at         timestamptz NOT NULL DEFAULT now(),
  completed_at        timestamptz,
  PRIMARY KEY (tenant_id, assignment_id),
  FOREIGN KEY (tenant_id, course_id) REFERENCES training_courses(tenant_id, course_id) ON DELETE RESTRICT,
  UNIQUE (tenant_id, assignment_id),
  -- 同一学员同一课程默认只有一个有效分配；补训走 REMEDIATION 状态而非新建
  UNIQUE (tenant_id, course_id, learner_subject_id),
  UNIQUE (tenant_id, idempotency_key),
  CHECK (status <> 'COMPLETED' OR completed_at IS NOT NULL)
);

CREATE TABLE training_questions (
  tenant_id        uuid NOT NULL,
  question_id      uuid NOT NULL,
  course_id        uuid NOT NULL,
  course_version   integer NOT NULL CHECK (course_version > 0),
  question_type    text NOT NULL CHECK (question_type IN ('SINGLE_CHOICE','MULTI_CHOICE','TRUE_FALSE','SHORT_ANSWER')),
  prompt           text NOT NULL,
  -- 客观题答案键；主观题题面与评分要点分离，API 层不得向学员返回 answer_key
  answer_key       jsonb,
  evidence_refs    jsonb NOT NULL DEFAULT '[]'::jsonb,
  status           text NOT NULL CHECK (status IN ('DRAFT','APPROVED','REVOKED')),
  created_at       timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, question_id),
  FOREIGN KEY (tenant_id, course_id) REFERENCES training_courses(tenant_id, course_id) ON DELETE CASCADE
);

CREATE TABLE training_attempts (
  tenant_id        uuid NOT NULL,
  attempt_id       uuid NOT NULL,
  assignment_id    uuid NOT NULL,
  course_version   integer NOT NULL CHECK (course_version > 0),
  responses        jsonb NOT NULL,
  objective_score  numeric(5,2) CHECK (objective_score IS NULL OR objective_score BETWEEN 0 AND 100),
  grading_status   text NOT NULL CHECK (grading_status IN ('PENDING','AUTO_GRADED','HUMAN_REVIEWED')),
  idempotency_key  text,
  submitted_at     timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, attempt_id),
  FOREIGN KEY (tenant_id, assignment_id) REFERENCES training_assignments(tenant_id, assignment_id) ON DELETE CASCADE,
  UNIQUE (tenant_id, assignment_id, idempotency_key)
);
COMMENT ON COLUMN training_attempts.objective_score IS '服务端按已批准题库版本判分；客户端提交的分数一律丢弃（TRAIN-EXAM-01）';

CREATE TABLE training_practical_signoffs (
  tenant_id            uuid NOT NULL,
  signoff_id           uuid NOT NULL,
  assignment_id        uuid NOT NULL,
  assessor_subject_id  text NOT NULL,
  decision             text NOT NULL CHECK (decision IN ('PASS','FAIL','RETRY')),
  evidence_uri         text,
  signed_at            timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, signoff_id),
  FOREIGN KEY (tenant_id, assignment_id) REFERENCES training_assignments(tenant_id, assignment_id) ON DELETE CASCADE
);

-- -----------------------------------------------------------------------------
-- 索引
-- -----------------------------------------------------------------------------
CREATE INDEX idx_training_courses_role      ON training_courses (tenant_id, role_id, status);
CREATE INDEX idx_training_sources_version   ON training_course_sources (tenant_id, knowledge_version_id);
CREATE INDEX idx_training_assign_learner    ON training_assignments (tenant_id, learner_subject_id, status);
CREATE INDEX idx_training_assign_cohort     ON training_assignments (tenant_id, cohort_id) WHERE cohort_id IS NOT NULL;
CREATE INDEX idx_training_questions_course  ON training_questions (tenant_id, course_id, course_version, status);
CREATE INDEX idx_training_attempts_assign   ON training_attempts (tenant_id, assignment_id, submitted_at DESC);
CREATE INDEX idx_training_signoffs_assign   ON training_practical_signoffs (tenant_id, assignment_id);

-- -----------------------------------------------------------------------------
-- 课程发布门禁：必须绑定已审批知识版本（§13.2）
-- -----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION fkh_guard_course_publish() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
  source_count integer;
  unapproved   integer;
BEGIN
  IF NEW.status = 'PUBLISHED' AND OLD.status IS DISTINCT FROM 'PUBLISHED' THEN
    SELECT count(*) INTO source_count
      FROM training_course_sources
     WHERE tenant_id = NEW.tenant_id AND course_id = NEW.course_id;

    IF source_count = 0 THEN
      RAISE EXCEPTION 'FKH_COURSE_NO_SOURCE: 课程发布必须绑定至少一个不可变知识版本 (course_id=%)', NEW.course_id
        USING ERRCODE = '23514';
    END IF;

    SELECT count(*) INTO unapproved
      FROM training_course_sources cs
      JOIN knowledge_versions kv
        ON kv.tenant_id = cs.tenant_id AND kv.id = cs.knowledge_version_id
     WHERE cs.tenant_id = NEW.tenant_id
       AND cs.course_id = NEW.course_id
       AND kv.status NOT IN ('APPROVED_INTERNAL','PUBLISHED_PUBLIC');

    IF unapproved > 0 THEN
      RAISE EXCEPTION 'FKH_COURSE_UNAPPROVED_SOURCE: 课程引用了 % 个未审批或已撤销知识版本 (course_id=%)',
        unapproved, NEW.course_id
        USING ERRCODE = '23514';
    END IF;

    NEW.published_at := now();
    NEW.version      := OLD.version + 1;
  END IF;

  RETURN NEW;
END;
$$;

CREATE TRIGGER trg_course_publish_guard
  BEFORE UPDATE ON training_courses
  FOR EACH ROW EXECUTE FUNCTION fkh_guard_course_publish();

-- -----------------------------------------------------------------------------
-- 知识撤销联动：引用该版本的已发布课程转 NEEDS_REVIEW（§13.2）
-- 同步执行，不依赖 outbox 消费者；消费者只做通知与审计补偿。
-- -----------------------------------------------------------------------------
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

CREATE TRIGGER trg_course_needs_review
  AFTER UPDATE OF status ON knowledge_versions
  FOR EACH ROW EXECUTE FUNCTION fkh_course_needs_review_on_revoke();

-- -----------------------------------------------------------------------------
-- 实操签核：签核人不得是学员本人（TRAIN-EXAM-02）
-- -----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION fkh_guard_signoff() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
  learner text;
BEGIN
  SELECT learner_subject_id INTO learner
    FROM training_assignments
   WHERE tenant_id = NEW.tenant_id AND assignment_id = NEW.assignment_id;

  IF learner IS NULL THEN
    RAISE EXCEPTION 'FKH_SIGNOFF_NO_ASSIGNMENT: 分配不存在或跨租户 (assignment_id=%)', NEW.assignment_id
      USING ERRCODE = '23503';
  END IF;

  IF NEW.assessor_subject_id = learner THEN
    RAISE EXCEPTION 'FKH_SIGNOFF_SELF: 学员不得自行签核实操 (assignment_id=%)', NEW.assignment_id
      USING ERRCODE = '23514';
  END IF;

  RETURN NEW;
END;
$$;

CREATE TRIGGER trg_signoff_guard
  BEFORE INSERT OR UPDATE ON training_practical_signoffs
  FOR EACH ROW EXECUTE FUNCTION fkh_guard_signoff();

-- -----------------------------------------------------------------------------
-- 学员视角题目视图：不含 answer_key
-- 列级 GRANT 由部署脚本按角色下发；API 层禁止直接查询基表。
-- -----------------------------------------------------------------------------
-- 关键：必须 security_invoker，否则视图以定义者身份读取并绕过基表 RLS。
CREATE VIEW v_training_questions_learner
  WITH (security_invoker = true) AS
SELECT tenant_id, question_id, course_id, course_version,
       question_type, prompt, evidence_refs, status
  FROM training_questions;
