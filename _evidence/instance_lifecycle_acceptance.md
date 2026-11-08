# 空环境首装 → 重启 → 备份恢复 验收记录

- 时间：2026-11-08T17:34:50.443546+00:00
- 基准：`08c2f0d`（ADR-009：一客户一套独立私域实例；共用 RAG 基座与 Knowledge API）
- 说明：全部使用**临时合成实例**；工厂演示（8010/8000/55432）与烘焙演示（8114/8104/55462）未触碰。

## 实例

| | 源实例 A | 恢复实例 B |
|---|---|---|
| 数据目录 | `instance-a` | `instance-b` |
| DB 端口 | 55471 | 55472 |
| API 端口 | 8141 | 8142 |
| 业务角色 | `fkh_app_acc_a`（非 owner，无 BYPASSRLS） | `fkh_app_acc_b` |
| JWT 密钥 | 实例独立（重启后稳定） | **新生成**（与 A 不通用） |
| 租户 | `a365654d-5efe-4b9b-bc4c-605947b21028` | `a365654d-5efe-4b9b-bc4c-605947b21028`（恢复） |

## 步骤与结果

| 步骤 | 结果 | 细节 |
|---|---|---|
| provision-a | ✅ | db_port=55471 api_port=8141 |
| app-role-minimal | ✅ | {"role": "fkh_app_acc_a", "rolsuper": false, "rolbypassrls": false, "rolcreatedb": false, "rolcreaterole": false, "table_owner": "postgres"} |
| serve-a | ✅ | http://127.0.0.1:8141 |
| import-and-publish | ✅ | 3 docs, version=bbbbf8ed… |
| agent-answer-with-citation | ✅ | status=ANSWERED citations=1 |
| old-evidence-locatable | ✅ | GET /v1/evidence → 200 |
| agent-cannot-ingest | ✅ | HTTP 403 |
| restart-token-and-config | ✅ | config/domains → 200, 4 个域 |
| restart-data-intact | ✅ | status=ANSWERED |
| restart-evidence-still-valid | ✅ | HTTP 200 |
| revoked-evidence-readable-before | ✅ | GET /v1/evidence → 200 |
| revoke-quality | ✅ | HTTP 200 |
| revoked-not-searchable | ✅ | search → 200, 命中 1 条，其中已撤销版本 0 条 |
| revoked-evidence-rejected | ✅ | GET /v1/evidence → 403 |
| revoked-not-answerable | ✅ | http=200 status=REFUSED |
| backup | ✅ | 181492 bytes |
| restore-new-instance | ✅ | db_port=55472 api_port=8142 secret=新生成 |
| cross-instance-token-rejected | ✅ | A 的令牌打 B → 401 |
| restored-valid-doc-answerable | ✅ | status=ANSWERED citations=1 |
| restored-revoked-evidence-rejected | ✅ | HTTP 403 |
| restored-valid-evidence-readable | ✅ | HTTP 200 |
| restored-revoked-not-searchable | ✅ | search → 200, 命中 1 条，已撤销版本 0 条 |
| restored-revoked-not-answerable | ✅ | http=200 status=REFUSED |
| restored-audit-retained | ✅ | {"rag_queries": 5, "evidence_access_log": 6, "knowledge_versions": 3, "catalog_items": 2, "quality_status": "REVOKED"} |
| cleanup | ✅ | 两套临时实例已停止（数据目录保留在 .acceptance/） |

## 实际命令（退出码）

```text
$ H:\MyWork\Factory-Knowledge-Hub\.venv\Scripts\python.exe -m fkh.tools.instance stop --workdir H:\MyWork\Factory-Knowledge-Hub\.acceptance\instance-a  → {"stopped": true, "api_was_running": false}
  exit=0
$ H:\MyWork\Factory-Knowledge-Hub\.venv\Scripts\python.exe -m fkh.tools.instance provision --workdir H:\MyWork\Factory-Knowledge-Hub\.acceptance\instance-a --db-port 55471 --api-port 8141 --db-name fkh_acc_a --name customer-a  → {"schema": 1, "name": "customer-a", "workdir": "H:\\MyWork\\Factory-Knowledge-Hub\\.acceptance\\instance-a", "pgdata": "H:\\MyWork\\Factory-Knowledge-Hub\\.acceptance\\instance-a\\pgdata", "db_port": 55471, "db_name": "fkh_acc_a", "admin_user": "postgres", "app_role": "fkh_app_acc_a", "api_port": 8141, "secret_dir": "secrets", "domains": ["product_catalog", "quality", "equipment_safety", "training"], "created_at": "2026-11-08T17:33:55.865061+00:00", "tenant_id": "a365654d-5efe-4b9b-bc4c-605947b21028", "ops_principal": "9358179f-0545-4fcc-a548-f17d69543044", "agent_principal": "fe1c0818-7322-41de-af8e-92f9d8faa0cd", "admin_dsn": "postgresql://postgres:***@127.0.0.1:55471/fkh_acc_a"}
  exit=0
$ H:\MyWork\Factory-Knowledge-Hub\.venv\Scripts\python.exe -m fkh.tools.instance serve --workdir H:\MyWork\Factory-Knowledge-Hub\.acceptance\instance-a  → {"api": "http://127.0.0.1:8141", "pid": 42504, "ready": true, "instance": "customer-a"}
  exit=0
$ H:\MyWork\Factory-Knowledge-Hub\.venv\Scripts\python.exe -m fkh.tools.instance stop --workdir H:\MyWork\Factory-Knowledge-Hub\.acceptance\instance-a  → {"stopped": true, "api_was_running": true}
  exit=0
$ H:\MyWork\Factory-Knowledge-Hub\.venv\Scripts\python.exe -m fkh.tools.instance serve --workdir H:\MyWork\Factory-Knowledge-Hub\.acceptance\instance-a  → {"api": "http://127.0.0.1:8141", "pid": 17812, "ready": true, "instance": "customer-a"}
  exit=0
$ H:\MyWork\Factory-Knowledge-Hub\.venv\Scripts\python.exe -m fkh.tools.instance backup --workdir H:\MyWork\Factory-Knowledge-Hub\.acceptance\instance-a --out H:\MyWork\Factory-Knowledge-Hub\.acceptance\instance-a.dump  → {"backup": "H:\\MyWork\\Factory-Knowledge-Hub\\.acceptance\\instance-a.dump", "bytes": 181492, "entries": 400}
  exit=0
$ H:\MyWork\Factory-Knowledge-Hub\.venv\Scripts\python.exe -m fkh.tools.instance stop --workdir H:\MyWork\Factory-Knowledge-Hub\.acceptance\instance-b  → {"stopped": true, "api_was_running": false}
  exit=0
$ H:\MyWork\Factory-Knowledge-Hub\.venv\Scripts\python.exe -m fkh.tools.instance restore --from H:\MyWork\Factory-Knowledge-Hub\.acceptance\instance-a.dump --workdir H:\MyWork\Factory-Knowledge-Hub\.acceptance\instance-b --db-port 55472 --api-port 8142 --db-name fkh_acc_b --name customer-b-restored  → {"schema": 1, "name": "customer-b-restored", "workdir": "H:\\MyWork\\Factory-Knowledge-Hub\\.acceptance\\instance-b", "pgdata": "H:\\MyWork\\Factory-Knowledge-Hub\\.acceptance\\instance-b\\pgdata", "db_port": 55472, "db_name": "fkh_acc_b", "admin_user": "postgres", "app_role": "fkh_app_acc_b", "api_port": 8142, "secret_dir": "secrets", "domains": ["equipment_safety", "product_catalog", "quality", "training"], "created_at": "2026-11-08T17:34:38.299698+00:00", "restored_from": "H:\\MyWork\\Factory-Knowledge-Hub\\.acceptance\\instance-a.dump", "tenant_id": "a365654d-5efe-4b9b-bc4c-605947b21028", "ops_principal": "9358179f-0545-4fcc-a548-f17d69543044", "agent_principal": "fe1c0818-7322-41de-af8e-92f9d8faa0cd", "admin_dsn": "postgresql://postgres:***@127.0.0.1:55472/fkh_acc_b"}
  exit=0
$ H:\MyWork\Factory-Knowledge-Hub\.venv\Scripts\python.exe -m fkh.tools.instance serve --workdir H:\MyWork\Factory-Knowledge-Hub\.acceptance\instance-b  → {"api": "http://127.0.0.1:8142", "pid": 25832, "ready": true, "instance": "customer-b-restored"}
  exit=0
$ H:\MyWork\Factory-Knowledge-Hub\.venv\Scripts\python.exe -m fkh.tools.instance stop --workdir H:\MyWork\Factory-Knowledge-Hub\.acceptance\instance-a  → {"stopped": true, "api_was_running": true}
  exit=0
$ H:\MyWork\Factory-Knowledge-Hub\.venv\Scripts\python.exe -m fkh.tools.instance stop --workdir H:\MyWork\Factory-Knowledge-Hub\.acceptance\instance-b  → {"stopped": true, "api_was_running": true}
  exit=0
```

## 恢复后审计计数（实例 B）

```json
{
  "rag_queries": 5,
  "evidence_access_log": 6,
  "knowledge_versions": 3,
  "catalog_items": 2
}
```

## 未覆盖的边界

1. 备份是**逻辑备份**（pg_dump 自定义格式），不含角色与授权；恢复时由 `instance restore` 重建业务角色，
   未验证跨大版本升级或物理备份（pg_basebackup）。
2. 备份为**在线一致性快照**，未验证「备份过程中持续写入」的恢复语义（未做 PITR）。
3. 恢复后的实例沿用同一 JWT 密钥格式但**新密钥**，未验证「密钥轮换后旧令牌失效」的策略。
4. 迁移只验证了当前最新版本；未演练从旧版本恢复到新版本（升级路径由 `docs/UPGRADE-DRILL.md` 另行覆盖）。
5. 未验证多租户在同一实例内的表现（产品边界是一客户一实例，不做多租户 SaaS）。
