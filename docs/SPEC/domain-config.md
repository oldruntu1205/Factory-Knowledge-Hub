# 知识域：实例配置（ADR-009 落地）

> 2026-11-08，迁移 `0035`。此前 8 个工厂域写死在库里（CHECK + API 枚举），
> 换行业上传必然 422——这是 ADR-009 里唯一"必须改内核才能用"的复用阻塞。

## 1. 模型

| 项 | 规则 |
|---|---|
| 存放 | `knowledge_domains(tenant_id, code, label, is_active, is_default)`，每实例一份 |
| 引用 | `knowledge_documents(tenant_id, domain)` **复合外键** → `knowledge_domains(tenant_id, code)`（RESTRICT，不级联删文档） |
| 格式 | `code` 匹配 `^[a-z][a-z0-9_]{1,63}$`；`label` 非空 |
| 种子 | 迁移只为**已有租户**插入工厂 8 域（`is_default=true`）；**新租户域清单为空**，导入前须初始化 |
| 授权 | 域**不参与**可见性与授权（仍由 status / audience / required_scope / ACL 决定）；域只是分类标签 |

## 2. 端点

| 端点 | scope | 语义 |
|---|---|---|
| `GET /v1/config/domains` | `knowledge.read` | 列出本实例域清单（含 `is_active` / `is_default`） |
| `POST /v1/config/domains` | `knowledge.write` | 登记（幂等）：首次 201；同 code 同 label → 200 `EXISTS`；同 code 不同 label → 409 |

租户一律取自认证上下文。`knowledge.write` 的含义明确为：「拥有知识写权限的人可以向本实例的
共享分类清单追加域」——本轮没有改名/停用/删除（生命周期策略暂不纳入本片）。

## 3. 迁移与回滚（**有前置条件的回滚**，L1 2026-11-08 确认）

- 存量数据：**不需要改写任何文档的 `domain` 值**（旧 8 域都在种子里）；
- 回滚（`migrations/rollback/0035_down.sql`）是**有条件回滚**：一旦存在旧 8 域之外的文档，
  先检测并 `RAISE EXCEPTION FKH_ROLLBACK_BLOCKED`（旧枚举 CHECK 无法容纳），
  不会出现"先删配置表再发现恢复不了"。

## 4. 验收

| 层次 | 证据 |
|---|---|
| 单元/集成 | `tests/e2e/test_domain_config.py`（7 条，真实 PG + 业务角色 + HTTP） |
| 复用 | `tests/e2e/test_two_instances.py::test_custom_domain_is_instance_configuration`：未登记 422 → 登记 201 → 导入 201 → 改名 409 |
| 实机 | `scripts/verify_domain_config_live.py` → `_evidence/domain_config_live.txt`（实例 B：登记 `roaster_maintenance` → 导入 → 审核发布 → 按内文作答并给出 2 条引用） |
| 库层 | 直接 SQL 插入未登记域 → `ForeignKeyViolation`（应用层只负责友好 422） |

## 5. 部署侧要求（新实例必须做的一步）

新租户域清单为空 → **导入前必须先登记域**。工厂实例可用模板（demo/`instance-up.ps1` 会登记 8 域），
其他行业实例登记自己的域清单即可，例如：

```powershell
# 幂等，可重复执行
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8104/v1/config/domains `
  -Headers @{ Authorization = "Bearer $token"; "Content-Type" = "application/json" } `
  -Body '{"code":"roaster_maintenance","label":"烘焙机维护"}'
```
