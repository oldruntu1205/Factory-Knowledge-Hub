# L1 终审：知识域实例配置落地（`fa36c86`）

- 日期：2026-11-08
- 风险归类：**DB schema 与迁移**、**对外 API 契约新增**、**校验口径变化**
- 设计评审回执：`_evidence/l1_reply_domain_config_20261108.md`（有条件通过，5 条 + 2 个契约细节）
- 请只做判断：**不要探索仓库、不要读源码、不要跑测试**。

## 1. 按你的条件逐条落地情况

| 你的要求 | 实现 |
|---|---|
| **复合外键**，禁止级联删除文档 | `knowledge_documents(tenant_id, domain)` → `knowledge_domains(tenant_id, code)`，`ON DELETE RESTRICT` |
| 已有租户补齐 8 域；新租户**不强制**工厂域 | 迁移只为已有租户插入 8 域（`is_default=true`）；新租户空清单，导入前须初始化 |
| **不要**"首次登记时顺便补工厂 8 域" | 未实现任何隐式补建；`demo_app`/`instance-up` 显式登记 8 域模板（代表部署时选择域清单），非运行时自动 |
| 域不参与可见性/授权 | 未改动任何可见性代码；新增用例 `test_domain_does_not_grant_visibility` |
| `knowledge.write` 可接受，租户取认证上下文 | 端点不接受请求体 tenant；仅读权限 → 403（用例覆盖） |
| 本轮不做停用/删除 | 未提供停用/删除端点 |
| **POST 幂等**：201 / 200 EXISTS / 409 改名 | 已实现，用例覆盖三种 |
| 迁移/回滚表述 | 文档写"无需改写存量文档 domain 值"；`0035_down` 先检测非旧 8 域文档，命中即 `RAISE EXCEPTION FKH_ROLLBACK_BLOCKED` |

## 2. 关键实现细节

- 表：`knowledge_domains(tenant_id, code, label, is_active, is_default, created_at)`，
  PK `(tenant_id, code)`，`code ~ '^[a-z][a-z0-9_]{1,63}$'`，RLS `tenant_isolation`（FORCE）。
- 文档侧：删旧枚举 CHECK，加格式 CHECK + 复合 FK。
- 应用层：`DomainService.assert_usable` → 未登记抛 `DomainMissing` → 422 且 message 含
  `POST /v1/config/domains` 提示；登记冲突抛 `DomainConflict` → 409（不静默改名）。
- 并发：`INSERT … ON CONFLICT DO NOTHING` + 回读校验，PK 兜底。

## 3. 验收证据

- `tests/e2e/test_domain_config.py`（7 条，真实 PG + 业务角色 + HTTP）：
  新租户空清单 → 未登记 422 → 登记 201 → 导入 201；已有租户 8 域；
  201/200/409 幂等；只读 403；SQL 插入未登记域 → `ForeignKeyViolation`；
  域不授予可见性；租户隔离。
- `tests/e2e/test_two_instances.py::test_custom_domain_is_instance_configuration`：
  未登记 422（含登记提示）→ 登记 201 → 导入 201 → 改名 409。
- 实机（实例 B，烘焙设备）：`scripts/verify_domain_config_live.py` →
  `_evidence/domain_config_live.txt`：登记 `roaster_maintenance` → 201，导入 → 201，
  审核/发布 → 200/200，问答 → `ANSWERED` + 2 条引用。
- 门禁：`uv run pytest -q` → **637 passed**（exit=0）；ruff / mypy 全绿。

## 4. 契约强制改写（按项目纪律登记，未放松断言）

1. `tests/e2e/test_document_api.py::test_validation_message_names_the_field`：
   domain 提示从"allowed values"改为"登记指引/格式"。仍断言：点名字段、给出可操作提示、
   不回显坏值（原三条断言不减少，仅替换第二条的措辞。
2. `tests/e2e/test_two_instances.py`：原"`bakery_equipment` → 422"用例改为
   "未登记 422 → 登记 201 → 导入 201 → 改名 409"，断言**更严**（新增登记与改名断言）。
3. `tests/conftest.py::create_tenant` 默认按工厂模板种子 8 域（新增 `seed_domains=False`
   用于验证空清单）——测试夹具，不代表生产隐式行为。

## 5. 需要你判断的四点

1. **新租户空清单**是否可接受？我认为这符合你的"不隐式补建"要求，但意味着
   部署脚本必须显式初始化域清单；若希望"部署模板强制跑一步"，需要我把它做成
   `instance-up` 的必选步骤并给出失败提示（当前已登记，但非强制）。
2. **`knowledge.write` 允许追加域**：我按你的确认实现。若后续要引入"配置管理角色"，
   我会另开 `config.domain.write`，不做静默替换。
3. **回滚的条件性**：`0035_down` 在有非旧 8 域文档时直接拒绝。这是否符合你对
   "回滚前先检测并拒绝"的要求？还是要求提供"把文档域迁移回旧 8 域"的辅助脚本？
4. **格式 CHECK 的上界 64**：我选了 `^[a-z][a-z0-9_]{1,63}$`（2–64 字符）。
   是否需要允许中文域 code（如 `烘焙设备`）？当前只允许 ASCII 小写标识符，label 才放中文。
