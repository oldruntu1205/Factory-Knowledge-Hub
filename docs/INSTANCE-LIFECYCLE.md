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
| 密钥 | `<workdir>/secrets/jwt.secret`：Windows 上为**仅当前账户的 NTFS DACL**（非继承），其它平台 0600；**重启后不变** |

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

`api.pid` 是 JSON（`pid` = 实际监听者，`launcher_pid` = 启动器，`nonce` = 本次启动随机串）。
`stop` 只有在「进程确实是本实例的 API」时才终止：nonce 命中，或 app/端口/监听者三重匹配；
pid 文件指向另一个服务时**拒绝停止，且不碰对方进程**；进程已不存在则视为陈旧 pid 文件并继续停 PG。

老实例的密钥如果只有 `chmod`，可显式收紧（需要以密钥所有者账户运行）：

```powershell
uv run python -m fkh.tools.instance harden-secrets --workdir .instance-customer-x
```

### 密钥轮换（JWT + DB 应用口令）

```powershell
uv run python -m fkh.tools.instance stop --workdir .instance-customer-x
uv run python -m fkh.tools.instance rotate-keys --workdir .instance-customer-x
uv run python -m fkh.tools.instance serve --workdir .instance-customer-x

# 宽限期结束（建议 ≥ 最长令牌有效期）后
uv run python -m fkh.tools.instance stop --workdir .instance-customer-x
uv run python -m fkh.tools.instance retire-jwt-previous --workdir .instance-customer-x
uv run python -m fkh.tools.instance serve --workdir .instance-customer-x
```

| 项 | 语义 |
|---|---|
| 前置 | `rotate-keys` / `retire-jwt-previous` 要求 API 已停止（避免新旧密钥并存期间状态不可解释） |
| JWT | 旧密钥写入 `secrets/jwt.previous.secret`，**只验签不签发**；新令牌用 `secrets/jwt.secret` 签；宽限期内旧令牌仍 200 |
| 二次轮换 | 宽限期旧密钥仍在时默认拒绝；确认要提前淘汰更早令牌才加 `--force`（会让更早一代令牌提前失效） |
| 退役 | `retire-jwt-previous` 删除旧密钥；**重启后**旧令牌立即 401 |
| DB | `ALTER ROLE <app_role> PASSWORD` 立即生效；旧应用口令马上失效；**管理员口令不在此命令轮换** |
| DB 失败语义 | `ALTER ROLE` 在显式事务中执行。服务端明确拒绝 → 删除 `.new`，旧口令不变；连接中断等**结果不确定** → 保留私有 `.new`、返回非 0、报告"DB 状态待确认"，不假装旧口令仍有效 |
| 待恢复文件 | `jwt.secret.new` / `jwt.previous.secret.new` / `db-app.password.new` 任一存在时，`serve` / `rotate-keys` / `retire-jwt-previous` 都会拒绝继续，必须先人工确认当前生效的密钥并处理 `.new` |
| 审计 | `retrieval_key_id` 随轮换更新为 `k-<name>-r<n>`；旧审计行的 key id 保持不变；`instance.json` 的 `rotation_history` 记录最近 20 条轮换事件（不含密钥） |
| 元数据 | `instance.json` 只记录 `jwt_key_id` / `retrieval_key_id` / 轮换时间 / 轮换事件，不含任何密钥 |

若 `rotate-keys` 报告"DB 口令变更结果待确认"，按以下顺序人工核查（不要直接覆盖 `.new`）：

1. 用旧 `db-app.password` 连一次业务库；用 `.new` 文件里的口令再连一次；
2. **新口令能连、旧口令不能连**：说明 ALTER 已生效，把 `.new` 替换成 `db-app.password`，重启；
3. **旧口令能连、新口令不能连**：说明 ALTER 未生效，删除 `.new`，重启后再重试轮换；
4. 两者都能连/都不能连：属于异常状态，先不要启动服务，检查 `pg_roles` / 连接串 / PG 日志。

> 轮换是"停服—换钥—重启"的确定性流程，不做在线双写；如果未来需要零停机，必须另立切片设计双密钥签发与连接池排空。


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
| 恢复目标 | **全新目录**（不存在或为空）：新数据目录、新端口、新库名；`restore` 重建业务角色并授权 |
| 失败清理 | 只撤销**本次创建**的库、角色、`pgdata`、`secrets`、`instance.json`；调用方预建的空目录保留；`CREATE DATABASE/ROLE` 已发出但客户端未拿到结果时**不按名字删除**，在清理回执与原异常 note 里报"可能遗留 + 人工核查" |
| 密钥 | 恢复实例**新生成**密钥（`force_new`），源实例令牌在恢复实例上不通用（401） |
| 前置 | 恢复前会初始化空集群并应用**同版本**迁移 schema（dump 自带 schema，不做迁移升级） |

## 4. 可复跑验收

```powershell
uv run python scripts/acceptance_instance_lifecycle.py
uv run python scripts/acceptance_instance_security_regressions.py
```

安全回归验收落 `_evidence/instance_security_regressions.md`，覆盖：Windows DACL + 普通账户
读拒绝、restore 全新目录/失败清理、自定义域重启、pid 指向外部服务时拒绝停止、冷启动首个
review 200。若运行验收的 Python 令牌不是提升令牌（如 DSH 低完整性沙箱），先用管理员
PowerShell 执行 `scripts/verify_secret_acl_windows.ps1 -SecretDir ... -OutJson ...`，
再用 `--acl-probe-json` 传入结果。

生命周期验收做三件事并落 `_evidence/instance_lifecycle_acceptance.md`（脱敏，不含密钥/口令）：

1. **空环境首装**：新目录 + 独立端口；只导入合成资料；迁移用管理员、API 用业务角色；
   Agent 只给 `answer/search/evidence.read`（越权导入必须 403）。
2. **重启同一实例**：原有数据、域配置、同一令牌仍可用；旧证据仍可读。
3. **撤销 → 备份 → 恢复到全新实例**：恢复实例用**自己的密钥**验证
   有效资料仍可答（带引用）、已撤销资料检索不到、撤销资料的旧引用 403、
   有效资料的旧引用仍可读、`rag_queries`/`evidence_access_log` 审计保留，
   且已撤销版本状态仍为 `REVOKED`。

验收默认把检索固定为 `lexical`、生成走本机 Ollama（`qwen3:14b`），因此**不依赖向量模型**，
但需要本机 Ollama 在跑（否则问答会 503，脚本会在该步失败并如实记录）。

## 5. 安全基线（2026-11-08 交付加固）

| 项 | 做法 |
|---|---|
| 数据库认证 | 交付实例 `initdb --auth-host=scram-sha-256`（临时 pwfile 初始化，用完即删）；**不用 trust** |
| 管理员与业务角色 | 各自独立口令（`secrets/db-admin.password` / `secrets/db-app.password`），交叉使用一律认证失败 |
| 业务角色权限 | `fkh_app_*`：LOGIN、非 owner、无 SUPERUSER、无 BYPASSRLS；只授 DML/序列/函数 |
| JWT 密钥 | 只在 `secrets/jwt.secret`：Windows 用**保护 DACL + 仅当前账户**（`winacl`），其它平台 0600；**`instance.json` 不含任何密钥/口令**；启动时校验长度与非占位值 |
| 凭据不外泄 | 口令只进环境变量与 PG 的 `PGPASSWORD`（不写 argv）；`initdb` 临时口令文件放在私有临时目录；日志/验收记录只出现脱敏 DSN；备份用 `--no-owner --no-privileges` |
| 重复首装 | 已初始化实例再次 `provision` **明确拒绝**；`--reuse` 幂等复用（保留租户/主体/数据/令牌） |
| 恢复边界 | `restore` 只接受**全新目录**（不存在或为空；端口空闲、库名不存在）；`pg_restore --single-transaction`；失败只清理本次创建的库/角色/目录，不删调用方预建目录 |
| 启动校验 | 端口占用直接拒绝并报占用者；启动后确认**本次子进程**存活 + `/health/ready` + 用本实例密钥做身份探测；域清单按**子集**校验，允许运行时登记的自定义域 |
| 停止校验 | pid 文件（JSON）记录实际监听者 + 启动器 + 本次 nonce；只有 nonce/app/端口/监听者匹配才终止；指向其它服务时拒绝且不碰对方；进程不存在则按陈旧 pid 处理 |
| 首个请求可见性 | 成功响应在**响应头发送前**提交事务（`CommitBeforeSend`），客户端紧随其后的读取/审核不会读到未提交状态 |
| 密钥轮换 | `rotate-keys`（停服）轮换 JWT + DB 应用口令；旧 JWT 进入宽限期，`retire-jwt-previous` 后旧令牌 401；管理员 DB 口令不在该命令内 |

**冷启动 review 403（已定位并修复，2026-11-09）**：不是租户绑定/连接池预热问题。实测复现：
`POST /documents` 返回 201 后 3.9ms 的 `POST /review` 得到 403，而数据库行在响应后 **158ms**
才可见。根因是 FastAPI 的 yield 依赖在 `await response(...)` **之后**才关闭 request exit stack
（`fastapi/routing.py: request_response`），事务提交被排到客户端已收到响应之后。修复为 ASGI
中间件在第一条 `http.response.start` 前提交成功响应的事务；错误响应仍回滚。验收条件：
冷启动后首个 `POST /v1/knowledge/{id}/review` 必须 200，**不再重试**。

## 6. 边界（未覆盖）

- 备份是**逻辑备份**：不含角色与授权，恢复由脚本重建；未验证 `pg_basebackup` 物理备份；
- 未做 PITR / 备份期间持续写入的恢复语义；
- 未验证跨大版本恢复（旧版备份恢复到新版），升级路径见 `docs/UPGRADE-DRILL.md`；
- 已实现停服式 JWT + DB 应用口令轮换；**未实现零停机双写**，也未把管理员 DB 口令纳入同一命令；
- 单实例内多租户不在产品范围（一客户一实例）；
- 交付包不含 `.acceptance/`（验收临时数据，已被 `.gitignore` 忽略）。
- `pg_hba.conf` 由 `initdb` 生成（host=scram，local=trust）；Windows 无 unix socket，本片未额外收紧 local 行；
  生产 Linux 部署需按部署规范收紧并由系统账户隔离。
- Windows 密钥 DACL 由**创建/恢复实例的账户**持有；换账户运行 `serve` 会拒绝（需以目标账户
  provision/restore，或由部署规范显式授权）。Linux 上由 `0600` + 系统账户隔离兜底。
- 启停身份校验依赖本工具写入的 pid 文件与本次 nonce；若被 systemd/容器包装，需要改 pidfile 语义。
- 未做长期并发压测；冷启动回归为 3 次重启 × 每轮 8 次上传+立即审核。
