-- 0026：经验采集（语音访谈）最小可信模型 —— 授权台账 / RAW 转写 / 带时间戳草稿
--
-- ADR-05 未签，本片只落"不依赖裁决口径"的部分：
--   1) 没有有效授权就不能建访谈（授权台账 + 前置校验）；
--   2) 转写落库为 RAW，**不进入 knowledge_chunks**，因此任何检索路径都看不到；
--   3) 草稿保留时间戳证据位，供人工审核；
--   4) 撤回授权 → 转写删除、草稿作废、访谈转 WITHDRAWN。
-- 不接真实 ASR（B1 待裁决），转写文本由调用方提供。

CREATE TABLE IF NOT EXISTS interview_consents (
  tenant_id        uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
  consent_id       uuid NOT NULL DEFAULT gen_random_uuid(),
  subject_id       text NOT NULL,                -- 受访者（外部主体标识）
  method           text NOT NULL CHECK (method IN ('ORAL_WITH_RECEIPT', 'WRITTEN')),
  receipt_uri      text,                          -- 告知书/签署件引用（不含正文）
  retention_days   integer NOT NULL CHECK (retention_days > 0 AND retention_days <= 3650),
  granted_by       text NOT NULL,                 -- 谁确认了授权（必须等于 subject_id：禁止代授权）
  granted_at       timestamptz NOT NULL DEFAULT now(),
  revoked_at       timestamptz,
  PRIMARY KEY (tenant_id, consent_id),
  CHECK (revoked_at IS NULL OR revoked_at >= granted_at),
  CHECK (granted_by = subject_id)
);

CREATE TABLE IF NOT EXISTS interviews (
  tenant_id       uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
  interview_id    uuid NOT NULL DEFAULT gen_random_uuid(),
  subject_id      text NOT NULL,
  consent_id      uuid NOT NULL,
  interviewer     text NOT NULL,
  language        text NOT NULL DEFAULT 'zh-CN',
  status          text NOT NULL CHECK (status IN (
                    'CONSENTED', 'TRANSCRIBED', 'DRAFT_READY', 'IN_REVIEW',
                    'APPROVED', 'REJECTED', 'WITHDRAWN')),
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, interview_id),
  FOREIGN KEY (tenant_id, consent_id) REFERENCES interview_consents(tenant_id, consent_id)
    ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS interview_transcripts (
  tenant_id      uuid NOT NULL,
  transcript_id  uuid NOT NULL DEFAULT gen_random_uuid(),
  interview_id   uuid NOT NULL,
  kind           text NOT NULL CHECK (kind IN ('MACHINE', 'HUMAN_EDITED')),
  content        text NOT NULL CHECK (length(btrim(content)) > 0),
  created_at     timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, transcript_id),
  FOREIGN KEY (tenant_id, interview_id) REFERENCES interviews(tenant_id, interview_id)
    ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS experience_drafts (
  tenant_id       uuid NOT NULL,
  draft_id        uuid NOT NULL DEFAULT gen_random_uuid(),
  interview_id    uuid NOT NULL,
  transcript_id   uuid NOT NULL,
  status          text NOT NULL CHECK (status IN (
                    'DRAFT', 'IN_REVIEW', 'APPROVED', 'REJECTED', 'WITHDRAWN')),
  claim           text NOT NULL,
  conditions      text,
  exceptions      text,
  risks           text,
  evidence_spans  jsonb NOT NULL DEFAULT '[]'::jsonb,   -- [{start_ms,end_ms,quote}]
  open_questions  jsonb NOT NULL DEFAULT '[]'::jsonb,
  reviewer        text,
  reviewed_at     timestamptz,
  review_note     text,
  created_at      timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, draft_id),
  FOREIGN KEY (tenant_id, interview_id) REFERENCES interviews(tenant_id, interview_id)
    ON DELETE CASCADE,
  FOREIGN KEY (tenant_id, transcript_id) REFERENCES interview_transcripts(tenant_id, transcript_id)
    ON DELETE CASCADE
);

COMMENT ON TABLE interview_transcripts IS
  'RAW 转写：不进入 knowledge_chunks，任何检索/问答路径都不可见（Rev2 §3.3/§8.4）';

CREATE INDEX IF NOT EXISTS idx_interviews_subject ON interviews (tenant_id, subject_id, status);
CREATE INDEX IF NOT EXISTS idx_drafts_interview ON experience_drafts (tenant_id, interview_id, status);

ALTER TABLE interview_consents     ENABLE ROW LEVEL SECURITY;
ALTER TABLE interview_consents     FORCE ROW LEVEL SECURITY;
ALTER TABLE interviews             ENABLE ROW LEVEL SECURITY;
ALTER TABLE interviews             FORCE ROW LEVEL SECURITY;
ALTER TABLE interview_transcripts  ENABLE ROW LEVEL SECURITY;
ALTER TABLE interview_transcripts  FORCE ROW LEVEL SECURITY;
ALTER TABLE experience_drafts      ENABLE ROW LEVEL SECURITY;
ALTER TABLE experience_drafts      FORCE ROW LEVEL SECURITY;

CREATE POLICY tenant_isolation ON interview_consents
  USING (tenant_id = fkh_current_tenant()) WITH CHECK (tenant_id = fkh_current_tenant());
CREATE POLICY tenant_isolation ON interviews
  USING (tenant_id = fkh_current_tenant()) WITH CHECK (tenant_id = fkh_current_tenant());
CREATE POLICY tenant_isolation ON interview_transcripts
  USING (tenant_id = fkh_current_tenant()) WITH CHECK (tenant_id = fkh_current_tenant());
CREATE POLICY tenant_isolation ON experience_drafts
  USING (tenant_id = fkh_current_tenant()) WITH CHECK (tenant_id = fkh_current_tenant());
