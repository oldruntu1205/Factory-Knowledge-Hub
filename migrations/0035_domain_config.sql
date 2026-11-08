-- 0035：知识域改为实例配置（去工厂硬编码；L1 2026-11-08 有条件通过）
--
-- 目标：不同行业的客户不需要改内核就能用自己的业务域。
-- 约束（按 L1 意见）：
--   1) 文档对域建立**复合外键**（tenant_id, domain）→ knowledge_domains(tenant_id, code)，
--      禁止级联删除文档；
--   2) 只为**已有租户**种子工厂 8 域；新租户域清单可以为空，导入前须由实例初始化登记；
--   3) 域只做组织/展示/过滤，不参与可见性与授权（可见性仍由 status/audience/scope/ACL 决定）。

CREATE TABLE IF NOT EXISTS knowledge_domains (
  tenant_id  uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
  code       text NOT NULL CHECK (code ~ '^[a-z][a-z0-9_]{1,63}$'),
  label      text NOT NULL CHECK (length(btrim(label)) > 0),
  is_active  boolean NOT NULL DEFAULT true,
  is_default boolean NOT NULL DEFAULT false,  -- 表示"由模板预置"，不是上传时的默认选项
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, code)
);

ALTER TABLE knowledge_domains ENABLE ROW LEVEL SECURITY;
ALTER TABLE knowledge_domains FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON knowledge_domains
  USING (tenant_id = fkh_current_tenant())
  WITH CHECK (tenant_id = fkh_current_tenant());

-- 已有租户：按工厂模板预置 8 域（保持历史行为）
INSERT INTO knowledge_domains (tenant_id, code, label, is_default)
SELECT t.id, d.code, d.label, true
  FROM tenants t
 CROSS JOIN (VALUES
   ('owner_experience',   '老师傅经验'),
   ('glass_materials',    '玻璃与材料'),
   ('production_process', '生产工艺'),
   ('product_catalog',    '产品目录'),
   ('quality',            '质量与检验'),
   ('equipment_safety',   '设备与安全'),
   ('foreign_trade',      '外贸客户'),
   ('training',           '培训材料')
 ) AS d(code, label)
ON CONFLICT (tenant_id, code) DO NOTHING;

-- 文档侧：枚举 CHECK → 格式 CHECK + 复合外键
ALTER TABLE knowledge_documents DROP CONSTRAINT IF EXISTS knowledge_documents_domain_check;
ALTER TABLE knowledge_documents
  ADD CONSTRAINT knowledge_documents_domain_format_check
  CHECK (domain ~ '^[a-z][a-z0-9_]{1,63}$');

ALTER TABLE knowledge_documents
  DROP CONSTRAINT IF EXISTS knowledge_documents_tenant_domain_fkey;
ALTER TABLE knowledge_documents
  ADD CONSTRAINT knowledge_documents_tenant_domain_fkey
  FOREIGN KEY (tenant_id, domain)
  REFERENCES knowledge_domains (tenant_id, code)
  ON UPDATE RESTRICT
  ON DELETE RESTRICT;

COMMENT ON TABLE knowledge_domains IS
  '实例级知识域清单：由部署初始化/运营登记；域只用于组织与过滤，不参与授权';
