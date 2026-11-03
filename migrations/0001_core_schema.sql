-- =============================================================================
-- Factory Knowledge Hub · 0001_core_schema.sql
-- 治理核心：租户、主体、知识文档、版本、切片、ACL、审核、检索审计、坏例、outbox
--
-- 依据：L1 架构契约 Rev.2 §5.2（最小 DDL）与 §5.2 末尾「DDL 实施前必须补强」
-- 本文件相对 §5.2 原始 DDL 的补强项，均以 [补强-N] 标注。
-- 前置：0000_extensions.sql
-- 回滚：rollback/0001_down.sql
-- =============================================================================

-- -----------------------------------------------------------------------------
-- 租户与主体
-- -----------------------------------------------------------------------------
CREATE TABLE tenants (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name        text NOT NULL,
  -- ADR-01 未签署前，部署区域写入配置而非数据库；此处仅记录逻辑区域标签
  region_tag  text,
  created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE principals (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id        uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
  external_subject text NOT NULL,
  principal_type   text NOT NULL CHECK (principal_type IN ('user','agent','service')),
  display_name     text,
  is_active        boolean NOT NULL DEFAULT true,
  created_at       timestamptz NOT NULL DEFAULT now(),
  -- [补强-1] 复合唯一键，作为所有 (tenant_id, principal_id) 外键的被引用键
  UNIQUE (tenant_id, external_subject),
  UNIQUE (tenant_id, id)
);
COMMENT ON TABLE principals IS '认证主体。external_subject 来自 OIDC sub；Agent 与 service 也在此登记，禁止共用用户身份';

-- -----------------------------------------------------------------------------
-- 知识文档与版本
-- -----------------------------------------------------------------------------
CREATE TABLE knowledge_documents (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id         uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
  knowledge_key     text NOT NULL,
  domain            text NOT NULL CHECK (domain IN (
                      'owner_experience','glass_materials','production_process',
                      'product_catalog','quality','equipment_safety','foreign_trade','training')),
  owner_department  text NOT NULL,
  created_at        timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, knowledge_key),
  UNIQUE (tenant_id, id)
);

CREATE TABLE knowledge_versions (
  id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id          uuid NOT NULL,
  document_id        uuid NOT NULL,
  version_no         integer NOT NULL CHECK (version_no > 0),
  title              text NOT NULL,
  classification     smallint NOT NULL CHECK (classification BETWEEN 0 AND 4),
  audience           text NOT NULL CHECK (audience IN ('internal','customer_public')),
  status             text NOT NULL CHECK (status IN (
                       'RAW','DRAFT','REVIEWED','APPROVED_INTERNAL',
                       'PUBLISHED_PUBLIC','SUPERSEDED','REVOKED')),
  content_sha256     text NOT NULL CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
  source_object_key  text NOT NULL,
  effective_at       timestamptz,
  expires_at         timestamptz,
  approved_by        uuid,
  public_approved_by uuid,
  created_at         timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (tenant_id, document_id) REFERENCES knowledge_documents(tenant_id, id) ON DELETE RESTRICT,
  -- [补强-1] 审批人必须是同租户主体
  FOREIGN KEY (tenant_id, approved_by)        REFERENCES principals(tenant_id, id) ON DELETE RESTRICT,
  FOREIGN KEY (tenant_id, public_approved_by) REFERENCES principals(tenant_id, id) ON DELETE RESTRICT,
  UNIQUE (tenant_id, document_id, version_no),
  UNIQUE (tenant_id, id),
  CHECK (expires_at IS NULL OR effective_at IS NULL OR expires_at > effective_at),
  -- §3.2：C0 不等于可公开，必须同时具备独立公开审批
  CHECK (status <> 'PUBLISHED_PUBLIC' OR (
    classification = 0 AND audience = 'customer_public'
    AND approved_by IS NOT NULL AND public_approved_by IS NOT NULL
    AND public_approved_by <> approved_by))
);
COMMENT ON TABLE knowledge_versions IS '不可变知识版本。内容一经创建不可修改，状态迁移与审批由触发器强制';

-- -----------------------------------------------------------------------------
-- 源资产（原始文档、录音）
-- -----------------------------------------------------------------------------
CREATE TABLE source_assets (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id         uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
  object_key        text NOT NULL,
  mime_type         text NOT NULL,
  sha256            text NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
  source_type       text NOT NULL CHECK (source_type IN ('document','audio','transcript','export')),
  consent_record_id text,
  retention_until   timestamptz,
  created_at        timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, object_key),
  UNIQUE (tenant_id, id)
);
COMMENT ON COLUMN source_assets.consent_record_id IS 'ADR-05 尚未签署；无授权记录时应用层必须拒绝 ASR 处理';

-- -----------------------------------------------------------------------------
-- ACL 策略（应用层为第一道防线，RLS 为第二道）
-- -----------------------------------------------------------------------------
CREATE TABLE acl_policies (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id   uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
  policy_json jsonb NOT NULL,
  revision    bigint NOT NULL DEFAULT 1 CHECK (revision > 0),
  updated_at  timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, id)
);
COMMENT ON COLUMN acl_policies.policy_json IS '形如 {"allow":{"roles":[],"groups":[],"departments":[],"subjects":[]},"deny":{...}}；Deny 优先于 Allow，缺省拒绝';

-- -----------------------------------------------------------------------------
-- 切片
-- -----------------------------------------------------------------------------
CREATE TABLE knowledge_chunks (
  id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id          uuid NOT NULL,
  version_id         uuid NOT NULL,
  parent_chunk_id    uuid,
  chunk_kind         text NOT NULL CHECK (chunk_kind IN ('parent','child')),
  chunk_text         text NOT NULL,
  -- [补强-5] 内容哈希与分词器版本，保证索引可复现重建
  chunk_sha256       text NOT NULL CHECK (chunk_sha256 ~ '^[0-9a-f]{64}$'),
  tokenizer_id       text NOT NULL,
  heading_path       text[] NOT NULL DEFAULT '{}',
  locator            jsonb NOT NULL,
  metadata           jsonb NOT NULL DEFAULT '{}'::jsonb,
  acl_policy_id      uuid NOT NULL,
  index_generation   bigint NOT NULL CHECK (index_generation >= 0),
  embedding_model_id text,
  -- [补强-6] 中文需由应用层分词器切分后写入；数据库只承载 simple 配置的装载结果
  lexeme             tsvector,
  created_at         timestamptz NOT NULL DEFAULT now(),
  updated_at         timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (tenant_id, version_id)    REFERENCES knowledge_versions(tenant_id, id) ON DELETE CASCADE,
  FOREIGN KEY (tenant_id, acl_policy_id) REFERENCES acl_policies(tenant_id, id)      ON DELETE RESTRICT,
  FOREIGN KEY (tenant_id, parent_chunk_id) REFERENCES knowledge_chunks(tenant_id, id) ON DELETE CASCADE,
  UNIQUE (tenant_id, id)
);
COMMENT ON COLUMN knowledge_chunks.metadata IS '承载 language / product_family / model_codes / parser_version / page_start / page_end；未知字段用显式 null，不得用 null 表示可公开';

-- -----------------------------------------------------------------------------
-- 审核动作（追加式审计，不可改不可删）
-- -----------------------------------------------------------------------------
CREATE TABLE review_actions (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id    uuid NOT NULL,
  version_id   uuid NOT NULL,
  actor_id     uuid NOT NULL,
  action       text NOT NULL CHECK (action IN (
                 'SUBMIT','REVIEW_PASS','REVIEW_REJECT','APPROVE_INTERNAL',
                 'APPROVE_PUBLIC','PUBLISH','SUPERSEDE','REVOKE')),
  reason       text,
  created_at   timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (tenant_id, version_id) REFERENCES knowledge_versions(tenant_id, id) ON DELETE CASCADE,
  FOREIGN KEY (tenant_id, actor_id)   REFERENCES principals(tenant_id, id)        ON DELETE RESTRICT,
  UNIQUE (tenant_id, id)
);

-- -----------------------------------------------------------------------------
-- 检索与回答审计
-- -----------------------------------------------------------------------------
CREATE TABLE rag_queries (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id    uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
  principal_id uuid NOT NULL,
  -- [补强-2] 主体引用改为复合外键，杜绝跨租户主体挂载
  query_hash   text NOT NULL,
  scope        text NOT NULL CHECK (scope IN ('customer_public','internal_training','internal_product')),
  status       text NOT NULL CHECK (status IN ('ANSWERED','REFUSED','HUMAN_REVIEW_REQUIRED','ERROR')),
  refusal_code text,
  trace_id     text NOT NULL,
  model_id     text,
  created_at   timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (tenant_id, principal_id) REFERENCES principals(tenant_id, id) ON DELETE RESTRICT,
  UNIQUE (tenant_id, id)
);
COMMENT ON COLUMN rag_queries.query_hash IS '仅存哈希，禁止落原始 query 明文（§6.3）';

CREATE TABLE rag_citations (
  id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id  uuid NOT NULL,
  query_id   uuid NOT NULL,
  chunk_id   uuid NOT NULL,
  version_id uuid NOT NULL,
  locator    jsonb NOT NULL,
  FOREIGN KEY (tenant_id, query_id)   REFERENCES rag_queries(tenant_id, id)          ON DELETE CASCADE,
  FOREIGN KEY (tenant_id, chunk_id)   REFERENCES knowledge_chunks(tenant_id, id)     ON DELETE RESTRICT,
  FOREIGN KEY (tenant_id, version_id) REFERENCES knowledge_versions(tenant_id, id)   ON DELETE RESTRICT,
  UNIQUE (tenant_id, id)
);

-- -----------------------------------------------------------------------------
-- 坏例与回归
-- -----------------------------------------------------------------------------
CREATE TABLE bad_cases (
  id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id          uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
  query_id           uuid,
  category           text NOT NULL,
  severity           text NOT NULL CHECK (severity IN ('P0','P1','P2')),
  redacted_case      jsonb NOT NULL,
  expected_behavior  jsonb NOT NULL,
  regression_status  text NOT NULL DEFAULT 'OPEN' CHECK (regression_status IN ('OPEN','FIXING','FIXED','WONTFIX')),
  owner_subject      text,
  root_cause         text,
  created_at         timestamptz NOT NULL DEFAULT now(),
  -- [补强-2] 复合外键
  FOREIGN KEY (tenant_id, query_id) REFERENCES rag_queries(tenant_id, id) ON DELETE SET NULL,
  UNIQUE (tenant_id, id)
);

-- -----------------------------------------------------------------------------
-- 事务 outbox
-- -----------------------------------------------------------------------------
CREATE TABLE outbox_events (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id         uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
  aggregate_type    text NOT NULL,
  aggregate_id      uuid NOT NULL,
  aggregate_version integer NOT NULL,
  event_type        text NOT NULL,
  payload           jsonb NOT NULL,
  occurred_at       timestamptz NOT NULL DEFAULT now(),
  published_at      timestamptz,
  retry_count       integer NOT NULL DEFAULT 0 CHECK (retry_count >= 0),
  UNIQUE (tenant_id, aggregate_type, aggregate_id, aggregate_version, event_type)
);

-- -----------------------------------------------------------------------------
-- 索引
-- -----------------------------------------------------------------------------
CREATE INDEX idx_documents_tenant_domain ON knowledge_documents (tenant_id, domain);
CREATE INDEX idx_versions_tenant_status  ON knowledge_versions (tenant_id, status);
CREATE INDEX idx_versions_tenant_expiry  ON knowledge_versions (tenant_id, expires_at) WHERE expires_at IS NOT NULL;
CREATE INDEX idx_chunks_tenant_version   ON knowledge_chunks (tenant_id, version_id, index_generation);
CREATE INDEX idx_chunks_parent           ON knowledge_chunks (tenant_id, parent_chunk_id) WHERE parent_chunk_id IS NOT NULL;
CREATE INDEX idx_chunks_lexeme           ON knowledge_chunks USING gin (lexeme);
CREATE INDEX idx_chunks_metadata         ON knowledge_chunks USING gin (metadata jsonb_path_ops);
CREATE INDEX idx_review_actions_version  ON review_actions (tenant_id, version_id, created_at);
CREATE INDEX idx_rag_queries_tenant_time ON rag_queries (tenant_id, created_at DESC);
CREATE INDEX idx_rag_queries_trace       ON rag_queries (trace_id);
CREATE INDEX idx_bad_cases_open          ON bad_cases (tenant_id, regression_status, severity) WHERE regression_status <> 'FIXED';
CREATE INDEX idx_outbox_pending          ON outbox_events (occurred_at) WHERE published_at IS NULL;

-- -----------------------------------------------------------------------------
-- [补强-3] 切片父子完整性：同租户、同版本、父块必须是 parent
-- -----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION fkh_assert_chunk_parent() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
  parent knowledge_chunks%ROWTYPE;
BEGIN
  IF NEW.parent_chunk_id IS NULL THEN
    RETURN NEW;
  END IF;

  IF NEW.chunk_kind <> 'child' THEN
    RAISE EXCEPTION 'FKH_CHUNK_PARENT_KIND: 只有 child 块可以设置 parent_chunk_id (chunk_id=%)', NEW.id
      USING ERRCODE = '23514';
  END IF;

  IF NEW.parent_chunk_id = NEW.id THEN
    RAISE EXCEPTION 'FKH_CHUNK_SELF_PARENT: 块不能以自身为父块 (chunk_id=%)', NEW.id
      USING ERRCODE = '23514';
  END IF;

  SELECT * INTO parent
    FROM knowledge_chunks
   WHERE tenant_id = NEW.tenant_id AND id = NEW.parent_chunk_id;

  IF NOT FOUND THEN
    RAISE EXCEPTION 'FKH_CHUNK_PARENT_MISSING: 父块不存在或跨租户 (parent_id=%)', NEW.parent_chunk_id
      USING ERRCODE = '23503';
  END IF;

  IF parent.chunk_kind <> 'parent' THEN
    RAISE EXCEPTION 'FKH_CHUNK_PARENT_NOT_PARENT: 父块 chunk_kind 必须为 parent (parent_id=%, kind=%)',
      parent.id, parent.chunk_kind
      USING ERRCODE = '23514';
  END IF;

  IF parent.version_id <> NEW.version_id THEN
    RAISE EXCEPTION 'FKH_CHUNK_PARENT_CROSS_VERSION: 父子块必须属于同一版本 (child=% parent=%)',
      NEW.version_id, parent.version_id
      USING ERRCODE = '23514';
  END IF;

  RETURN NEW;
END;
$$;

CREATE TRIGGER trg_chunk_parent_guard
  BEFORE INSERT OR UPDATE OF parent_chunk_id, chunk_kind, version_id
  ON knowledge_chunks
  FOR EACH ROW EXECUTE FUNCTION fkh_assert_chunk_parent();

-- -----------------------------------------------------------------------------
-- [补强-4] 版本不可变 + 状态机白名单 + 独立公开审批
-- -----------------------------------------------------------------------------
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

  -- 2) 状态迁移白名单（§3.2）
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
  IF NEW.status IN ('RAW','DRAFT') AND (NEW.approved_by IS NOT NULL OR NEW.public_approved_by IS NOT NULL) THEN
    RAISE EXCEPTION 'FKH_APPROVER_PREMATURE: 状态 % 不得存在审批人记录 (version_id=%)', NEW.status, NEW.id
      USING ERRCODE = '23514';
  END IF;

  RETURN NEW;
END;
$$;

CREATE TRIGGER trg_version_guard
  BEFORE UPDATE ON knowledge_versions
  FOR EACH ROW EXECUTE FUNCTION fkh_guard_version_mutation();

CREATE OR REPLACE FUNCTION fkh_block_delete() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'FKH_APPEND_ONLY: 表 % 为追加式记录，不允许 DELETE；请使用撤销状态', TG_TABLE_NAME
    USING ERRCODE = '42501';
END;
$$;

-- 版本只能撤销，不能物理删除
CREATE TRIGGER trg_version_no_delete
  BEFORE DELETE ON knowledge_versions
  FOR EACH ROW EXECUTE FUNCTION fkh_block_delete();

-- 审核动作不可改不可删
CREATE TRIGGER trg_review_actions_no_update
  BEFORE UPDATE ON review_actions
  FOR EACH ROW EXECUTE FUNCTION fkh_block_delete();

CREATE TRIGGER trg_review_actions_no_delete
  BEFORE DELETE ON review_actions
  FOR EACH ROW EXECUTE FUNCTION fkh_block_delete();

-- -----------------------------------------------------------------------------
-- updated_at 维护
-- -----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION fkh_touch_updated_at() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  NEW.updated_at := now();
  RETURN NEW;
END;
$$;

CREATE TRIGGER trg_chunks_touch
  BEFORE UPDATE ON knowledge_chunks
  FOR EACH ROW EXECUTE FUNCTION fkh_touch_updated_at();

CREATE TRIGGER trg_acl_touch
  BEFORE UPDATE ON acl_policies
  FOR EACH ROW EXECUTE FUNCTION fkh_touch_updated_at();
