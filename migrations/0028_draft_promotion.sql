-- 0028：经验草稿 → 知识版本的晋升链路
--
-- 目的（Rev2 §3.3）：人工审核通过的访谈经验要能进入知识治理流程（草稿→审核→发布），
-- 而不是停在 experience_drafts 里；同时保留"它来自哪次访谈"的反向链接，
-- 使撤回授权时可以连带处理已晋升的知识（撤下而非静默保留）。

ALTER TABLE experience_drafts
  ADD COLUMN IF NOT EXISTS promoted_version_id uuid,
  ADD COLUMN IF NOT EXISTS promoted_at timestamptz;

ALTER TABLE experience_drafts
  DROP CONSTRAINT IF EXISTS experience_drafts_tenant_id_promoted_version_id_fkey;

-- 版本被撤销/删除时只清空链接，不删草稿（审计要能回答"曾经晋升过什么"）
ALTER TABLE experience_drafts
  ADD CONSTRAINT experience_drafts_tenant_id_promoted_version_id_fkey
  FOREIGN KEY (tenant_id, promoted_version_id)
  REFERENCES knowledge_versions (tenant_id, id)
  ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_drafts_promoted_version
  ON experience_drafts (tenant_id, promoted_version_id);
