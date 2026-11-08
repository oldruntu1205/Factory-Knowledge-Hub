# 客户实例交付加固验收（P0 认证/密钥、P1 首装与恢复、P1 启停、恢复审计核对）

- 时间：2026-11-08T16:18:05.656966+00:00
- 基准：`0c2fd5f`（本轮加固前）；工厂/烘焙演示环境未触碰
- 密钥与口令**不在本记录中**（只记录路径与校验结论）

## 实例

| | A（首装） | B（恢复） |
|---|---|---|
| 数据目录 | `h-a` | `h-b` |
| DB/API 端口 | 55481/8151 | 55482/8152 |
| 业务角色 | `fkh_app_h_a` | `fkh_app_h_b` |
| 租户 | `84d6b75f-0934-459b-b800-ce84adee7741` | `84d6b75f-0934-459b-b800-ce84adee7741` |

## 步骤

| 步骤 | 结果 | 细节 |
|---|---|---|
| baseline-trust-login-accepted | ✅ | 修复前的演示式集群（initdb -A trust）无口令即可登录管理员 —— 这是本片要消除的行为 |
| provision-a | ✅ | tenant=84d6b75f-0934-459b-b800-ce84adee7741 |
| instance-json-has-no-secret | ✅ | 命中：无 |
| secret-files-present | ✅ | {"jwt.secret": 50, "db-admin.password": 50, "db-app.password": 50} |
| db-auth-isolation | ✅ | {"admin_ok": true, "admin_no_pw": false, "admin_with_app_pw": false, "app_ok": true, "app_with_admin_pw": false} |
| business-role-not-owner | ✅ | {"name": "fkh_app_h_a", "rolsuper": false, "rolbypassrls": false, "table_owner": "postgres"} |
| serve-rejects-weak-secret | ✅ | exit=1 |
| provision-refuses-reinit | ✅ | exit=1 |
| provision-reuse-idempotent | ✅ | tenant 保持 84d6b75f-0934-459b-b800-ce84adee7741 |
| serve-refuses-busy-port | ✅ | API 端口 8151 已被 pid=28608（python.exe）占用；实例之间端口不复用，请换端口或先停掉占用者 |
| serve-identity-ok | ✅ | config/domains → 200 |
| stop-refuses-foreign-pid | ✅ | exit=1 |
| valid-evidence-readable-before | ✅ | HTTP 200 |
| revoke-quality | ✅ | HTTP 200 |
| revoke-status-settled | ✅ | status=REVOKED |
| audit-snapshot-before-backup | ✅ | {"query_id": "fc19ddfa-daaa-4ead-a4d2-70c61acfcf7a", "query_hash": "0d073430a31ba8405c6161040f9d5d832070d4687b1bb45fc72fb266ef7b9e23", "evidence_log_id": "a932a96c-a54e-44c3-a141-68afc44183e7", "evidence_log_evidence_id": "69fb595c-2305-4277-b83f-2af81a6743af", "version_status": "REVOKED", "counts": {"rag_queries": 1, "evidence_access_log": 1, "knowledge_versions": 3}} |
| backup-ok | ✅ | 180755 bytes |
| restore-to-fresh-instance | ✅ | tenant 相同；新密钥=True |
| audit-ids-survive-restore | ✅ | {"query": "fc19ddfa-daaa-4ead-a4d2-70c61acfcf7a", "evidence_log": "a932a96c-a54e-44c3-a141-68afc44183e7", "version_status": "REVOKED", "counts_after": {"rag_queries": 1, "evidence_access_log": 1, "knowledge_versions": 3}} |
| restored-role-not-owner | ✅ | {"rolsuper": false, "rolbypassrls": false, "table_owner": "postgres"} |
| restored-ops-token-ok | ✅ | config/domains → 200 |
| old-instance-token-rejected | ✅ | A 的令牌打 B → 401 |
| restored-valid-answerable | ✅ | status=ANSWERED |
| restored-valid-evidence-ok | ✅ | HTTP 200 |
| restored-revoked-evidence-rejected | ✅ | HTTP 403 |
| restored-revoked-not-searchable | ✅ | 命中 0 条 |
| restored-revoked-not-answerable | ✅ | status=REFUSED |
| bad-restore-fails-clean | ✅ | {"instance_json": false, "workdir": false, "api_port_busy": false} |
| source-instance-and-backup-intact | ✅ | A 仍 200；原备份仍可读（pg_restore --list exit=0） |
| stop-verified | ✅ | B API 端口已关闭=True |

## 命令（退出码）

```text
$ H:\MyWork\Factory-Knowledge-Hub\.venv\Scripts\python.exe -m fkh.tools.instance stop --workdir H:\MyWork\Factory-Knowledge-Hub\.acceptance\h-a  → {"stopped": true, "api_was_running": false}
  exit=0
$ H:\MyWork\Factory-Knowledge-Hub\.venv\Scripts\python.exe -m fkh.tools.instance stop --workdir H:\MyWork\Factory-Knowledge-Hub\.acceptance\h-b  → {"stopped": true, "api_was_running": false}
  exit=0
$ H:\MyWork\Factory-Knowledge-Hub\.venv\Scripts\python.exe -m fkh.tools.instance provision --workdir H:\MyWork\Factory-Knowledge-Hub\.acceptance\h-a --db-port 55481 --api-port 8151 --db-name fkh_h_a --name hard-a  → {"schema": 1, "name": "hard-a", "workdir": "H:\\MyWork\\Factory-Knowledge-Hub\\.acceptance\\h-a", "pgdata": "H:\\MyWork\\Factory-Knowledge-Hub\\.acceptance\\h-a\\pgdata", "db_port": 55481, "db_name": "fkh_h_a", "admin_user": "postgres", "app_role": "fkh_app_h_a", "api_port": 8151, "secret_dir": "secrets", "domains": ["product_catalog", "quality", "equipment_safety", "training"], "created_at": "2026-11-08T16:16:42.668029+00:00", "tenant_id": "84d6b75f-0934-459b-b800-ce84adee7741", "ops_principal": "c6adae8f-4c48-4c05-8e54-981f672ae3a7", "agent_principal": "591e0d18-7718-44ac-bf21-4db1ddb84cdd", "admin_dsn": "postgresql://postgres:***@127.0.0.1:55481/fkh_h_a"}
  exit=0
$ H:\MyWork\Factory-Knowledge-Hub\.venv\Scripts\python.exe -m fkh.tools.instance status --workdir H:\MyWork\Factory-Knowledge-Hub\.acceptance\h-a  → {"name": "hard-a", "api_port": 8151, "db_port": 55481, "tenant_id": "84d6b75f-0934-459b-b800-ce84adee7741", "counts": {"knowledge_documents": 0, "knowledge_versions": 0, "knowledge_chunks": 0, "catalog_items": 0, "rag_queries": 0, "evidence_access_log": 0}, "versions": {}, "app_role": {"name": "fkh_app_h_a", "rolsuper": false, "rolbypassrls": false, "table_owner": "postgres"}}
  exit=0
$ H:\MyWork\Factory-Knowledge-Hub\.venv\Scripts\python.exe -m fkh.tools.instance serve --workdir H:\MyWork\Factory-Knowledge-Hub\.acceptance\h-a
  exit=1
$ H:\MyWork\Factory-Knowledge-Hub\.venv\Scripts\python.exe -m fkh.tools.instance provision --workdir H:\MyWork\Factory-Knowledge-Hub\.acceptance\h-a --db-port 55481 --api-port 8151 --db-name fkh_h_a --name hard-a
  exit=1
$ H:\MyWork\Factory-Knowledge-Hub\.venv\Scripts\python.exe -m fkh.tools.instance provision --workdir H:\MyWork\Factory-Knowledge-Hub\.acceptance\h-a --db-port 55481 --api-port 8151 --db-name fkh_h_a --name hard-a --reuse  → {"reused": true, "workdir": "H:\\MyWork\\Factory-Knowledge-Hub\\.acceptance\\h-a", "tenant_id": "84d6b75f-0934-459b-b800-ce84adee7741"}
  exit=0
$ H:\MyWork\Factory-Knowledge-Hub\.venv\Scripts\python.exe -m fkh.tools.instance serve --workdir H:\MyWork\Factory-Knowledge-Hub\.acceptance\h-a
  exit=1
$ H:\MyWork\Factory-Knowledge-Hub\.venv\Scripts\python.exe -m fkh.tools.instance serve --workdir H:\MyWork\Factory-Knowledge-Hub\.acceptance\h-a  → {"api": "http://127.0.0.1:8151", "pid": 13888, "ready": true, "instance": "hard-a"}
  exit=0
$ H:\MyWork\Factory-Knowledge-Hub\.venv\Scripts\python.exe -m fkh.tools.instance stop --workdir H:\MyWork\Factory-Knowledge-Hub\.acceptance\h-a
  exit=1
$ H:\MyWork\Factory-Knowledge-Hub\.venv\Scripts\python.exe -m fkh.tools.instance backup --workdir H:\MyWork\Factory-Knowledge-Hub\.acceptance\h-a --out H:\MyWork\Factory-Knowledge-Hub\.acceptance\h-a.dump  → {"backup": "H:\\MyWork\\Factory-Knowledge-Hub\\.acceptance\\h-a.dump", "bytes": 180755, "entries": 400}
  exit=0
$ H:\MyWork\Factory-Knowledge-Hub\.venv\Scripts\python.exe -m fkh.tools.instance restore --from H:\MyWork\Factory-Knowledge-Hub\.acceptance\h-a.dump --workdir H:\MyWork\Factory-Knowledge-Hub\.acceptance\h-b --db-port 55482 --api-port 8152 --db-name fkh_h_b --name hard-b  → {"schema": 1, "name": "hard-b", "workdir": "H:\\MyWork\\Factory-Knowledge-Hub\\.acceptance\\h-b", "pgdata": "H:\\MyWork\\Factory-Knowledge-Hub\\.acceptance\\h-b\\pgdata", "db_port": 55482, "db_name": "fkh_h_b", "admin_user": "postgres", "app_role": "fkh_app_h_b", "api_port": 8152, "secret_dir": "secrets", "domains": ["equipment_safety", "product_catalog", "quality", "training"], "created_at": "2026-11-08T16:17:08.902863+00:00", "restored_from": "H:\\MyWork\\Factory-Knowledge-Hub\\.acceptance\\h-a.dump", "tenant_id": "84d6b75f-0934-459b-b800-ce84adee7741", "ops_principal": "c6adae8f-4c48-4c05-8e54-981f672ae3a7", "agent_principal": "591e0d18-7718-44ac-bf21-4db1ddb84cdd", "admin_dsn": "postgresql://postgres:***@127.0.0.1:55482/fkh_h_b"}
  exit=0
$ H:\MyWork\Factory-Knowledge-Hub\.venv\Scripts\python.exe -m fkh.tools.instance serve --workdir H:\MyWork\Factory-Knowledge-Hub\.acceptance\h-b  → {"api": "http://127.0.0.1:8152", "pid": 41972, "ready": true, "instance": "hard-b"}
  exit=0
$ H:\MyWork\Factory-Knowledge-Hub\.venv\Scripts\python.exe -m fkh.tools.instance restore --from H:\MyWork\Factory-Knowledge-Hub\.acceptance\broken.dump --workdir H:\MyWork\Factory-Knowledge-Hub\.acceptance\h-c --db-port 55483 --api-port 8153 --db-name fkh_h_c --name hard-c
  exit=1
$ H:\MyWork\Factory-Knowledge-Hub\.venv\Scripts\python.exe -m fkh.tools.instance stop --workdir H:\MyWork\Factory-Knowledge-Hub\.acceptance\h-b  → {"stopped": true, "api_was_running": true}
  exit=0
$ H:\MyWork\Factory-Knowledge-Hub\.venv\Scripts\python.exe -m fkh.tools.instance stop --workdir H:\MyWork\Factory-Knowledge-Hub\.acceptance\h-a  → {"stopped": true, "api_was_running": true}
  exit=0
```

## 备份前审计快照（恢复后按 ID 核对）

```json
{
  "query_id": "fc19ddfa-daaa-4ead-a4d2-70c61acfcf7a",
  "query_hash": "0d073430a31ba8405c6161040f9d5d832070d4687b1bb45fc72fb266ef7b9e23",
  "evidence_log_id": "a932a96c-a54e-44c3-a141-68afc44183e7",
  "evidence_log_evidence_id": "69fb595c-2305-4277-b83f-2af81a6743af",
  "version_status": "REVOKED",
  "counts": {
    "rag_queries": 1,
    "evidence_access_log": 1,
    "knowledge_versions": 3
  }
}
```

## 剩余边界

1. 备份仍为逻辑备份（`pg_dump --format=custom --no-owner --no-privileges`），
   不含角色与其口令；恢复时由 `restore` 重建业务角色并生成**新口令与新 JWT 密钥**。
2. 未做 PITR；未验证备份期间持续写入后的时间点一致性。
3. 未验证跨大版本恢复（旧 dump 恢复到新 schema）。
4. 口令与密钥的**静态保护**依赖操作系统访问控制（本机为 NTFS ACL）；
   脚本只做 `chmod 0600` 尽力而为，未在 Linux 上做独立验证。
5. `pg_hba.conf` 由 initdb 生成（host=scram-sha-256，local=trust）；Windows 无 unix socket，
   本片未额外收紧 local 行（生产 Linux 部署需按部署规范收紧并加 md5/scram）。
6. 启停的进程身份校验基于 cmdline 子串匹配，进程树被替换/包装（如 systemd 包装）需另配 pidfile 语义。
