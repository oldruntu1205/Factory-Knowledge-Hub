# 客户实例生命周期：首装 / 重启 / 备份恢复

> 交付形态（ADR-009）：**一客户一套独立私域实例**，共用同一套 RAG 基座与 Knowledge API。
> 本文是运维入口；演示播种（`fkh.tools.demo_app`）与交付入口（`fkh.tools.instance`）**完全分离**。

## 1. 一条命令首装

```powershell
uv run python -m fkh.tools.instance provision `
    --workdir .instance-customer-x `
    --db-port 55501 --api-port 8151 `
    --db-name fkh_customer_x --name customer-x
```

它做的事（每步都可核对）：

| 步骤 | 说明 |
|---|---|
| 内嵌 PG | 在 `<workdir>/pgdata` 初始化独立数据目录，只监听 `127.0.0.1:<db-port>` |
| 建库 | `CREATE DATABASE <db-name>`（管理员 `postgres` 执行） |
| 迁移 | `migrations.apply` 用**管理员**连接执行，业务角色不参与 DDL |
| 业务角色 | `fkh_app_<suffix>`：LOGIN、**非 owner**、无 SUPERUSER、无 BYPASSRLS；只授 DML/序列/函数 |
| 租户与主体 | 一个租户 + 一个运维 user 主体 + 一个 agent 主体（无演示语料） |
| 实例配置 | 登记 4 个域与目录规则（`^[A-Z]{2}-[0-9]{4}$` / `CNY`），不依赖运行时隐式补建 |
| 密钥 | `<workdir>/secrets/jwt.secret`（0600），**重启后不变** |

输出 `instance.json`（含 `admin_dsn` / `app_dsn` / 端口 / 租户 / 主体），密钥单独放在 secret 文件里。

> 端口必须显式给、且两套实例不复用；`provision` 会拒绝已被占用的端口。

## 2. 启动 / 停止

```powershell
uv run python -m fkh.tools.instance serve --workdir .instance-customer-x   # 起 PG + API，等 /health/ready
uv run python -m fkh.tools.instance stop  --workdir .instance-customer-x   # 停 API 与内嵌 PG
```

`serve` 用 **`app_dsn`（业务角色）** 启动 API，并用实例 secret 注入 `FKH_JWT_SECRET`；
**重启后同一令牌仍然有效**（密钥来自实例目录，不重新生成）。
`serve` 会顺带把内嵌 PG 拉起来（`stop` 会连库一起停）。

## 3. 备份与恢复

```powershell
uv run python -m fkh.tools.instance backup --workdir .instance-customer-x --out backup-x.dump

uv run python -m fkh.tools.instance restore --from backup-x.dump `
    --workdir .instance-customer-y --db-port 55502 --api-port 8152 `
    --db-name fkh_customer_y --name customer-y
```

| 项 | 语义 |
|---|---|
| 备份格式 | `pg_dump --format=custom --no-owner --no-privileges`（逻辑备份，不含角色/授权） |
| 恢复目标 | **全新隔离实例**：新数据目录、新端口、新库名；`restore` 重建业务角色并授权 |
| 密钥 | 恢复实例**新生成**密钥（`force_new`），源实例令牌在恢复实例上不通用（401） |
| 前置 | 恢复前会初始化空集群并应用**同版本**迁移 schema（dump 自带 schema，不做迁移升级） |

## 4. 可复跑验收

```powershell
uv run python scripts/acceptance_instance_lifecycle.py
```

做三件事并落 `_evidence/instance_lifecycle_acceptance.md`（脱敏，不含密钥/口令）：

1. **空环境首装**：新目录 + 独立端口；只导入合成资料；迁移用管理员、API 用业务角色；
   Agent 只给 `answer/search/evidence.read`（越权导入必须 403）。
2. **重启同一实例**：原有数据、域配置、同一令牌仍可用；旧证据仍可读。
3. **撤销 → 备份 → 恢复到全新实例**：恢复实例用**自己的密钥**验证
   有效资料仍可答（带引用）、已撤销资料检索不到、撤销资料的旧引用 403、
   有效资料的旧引用仍可读、`rag_queries`/`evidence_access_log` 审计保留，
   且已撤销版本状态仍为 `REVOKED`。

验收默认把检索固定为 `lexical`、生成走本机 Ollama（`qwen3:14b`），因此**不依赖向量模型**，
但需要本机 Ollama 在跑（否则问答会 503，脚本会在该步失败并如实记录）。

## 5. 边界（未覆盖）

- 备份是**逻辑备份**：不含角色与授权，恢复由脚本重建；未验证 `pg_basebackup` 物理备份；
- 未做 PITR / 备份期间持续写入的恢复语义；
- 未验证跨大版本恢复（旧版备份恢复到新版），升级路径见 `docs/UPGRADE-DRILL.md`；
- 未验证密钥轮换策略（恢复实例换密钥后，旧令牌自然是 401，但没有轮换流程）；
- 单实例内多租户不在产品范围（一客户一实例）；
- 交付包不含 `.acceptance/`（验收临时数据，已被 `.gitignore` 忽略）。
