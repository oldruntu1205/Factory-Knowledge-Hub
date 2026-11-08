# 实例密钥轮换验收（JWT 宽限期 + DB 应用口令）

- 时间：2026-11-08T17:59:14.731178+00:00
- 本记录不含任何密钥/口令内容；只记录状态、路径、退出码。

| 步骤 | 结果 | 细节 |
|---|---|---|
| old-token-before-rotation | ✅ | 200 |
| old-db-password-before-rotation | ✅ | 可以登录 |
| rotate-keys | ✅ | {"rotated": true, "jwt_key_id": "jwt-2", "retrieval_key_id": "k-rotate-a-r1", "previous_grace": true, "restart_required": true, "db_app_rotated": true} |
| jwt-current-changed | ✅ |  |
| jwt-previous-keeps-old-secret | ✅ |  |
| db-app-password-changed | ✅ |  |
| instance-json-has-key-ids | ✅ | {"jwt_key_id": "jwt-2", "retrieval_key_id": "k-rotate-a-r1"} |
| previous-secret-acl-private | ✅ |  |
| instance-json-has-no-secret | ✅ |  |
| old-token-during-grace | ✅ | 200 |
| new-token-during-grace | ✅ | 200 |
| old-db-password-rejected | ✅ | 旧口令失败 |
| new-db-password-works | ✅ | 新口令成功 |
| retire-jwt-previous | ✅ | {"retired": true, "restart_required": true} |
| previous-file-removed | ✅ |  |
| old-token-after-retire | ✅ | 401 |
| new-token-after-retire | ✅ | 200 |

## 命令（退出码）

```text
$ H:\MyWork\Factory-Knowledge-Hub\.venv\Scripts\python.exe -m fkh.tools.instance provision --workdir H:\MyWork\Factory-Knowledge-Hub\.acceptance\rotate-a --db-port 55499 --api-port 8169 --db-name fkh_rotate_a --name rotate-a  → exit=0
  out: 已应用 36 个迁移。
  out: {"schema": 1, "name": "rotate-a", "workdir": "H:\\MyWork\\Factory-Knowledge-Hub\\.acceptance\\rotate-a", "pgdata": "H:\\MyWork\\Factory-Knowledge-Hub\\.acceptance\\rotate-a\\pgdata", "db_port": 55499, "db_name": "fkh_rotate_a", "admin_user": "postgres", "app_role": "fkh_app_rotate_a", "api_port": 8169, "secret_dir": "secrets", "domains": ["product_catalog", "quality", "equipment_safety", "training"], "created_at": "2026-11-08T17:58:57.185828+00:00", "jwt_key_id": "jwt-1", "retrieval_key_id": "k-rotate-a", "jwt_rotated_at": "2026-11-08T17:58:57.185828+00:00", "db_app_rotated_at": "2026-11-08T17:58:57.185828+00:00", "rotation_seq": 0, "tenant_id": "a9b0cb5c-b558-40fe-bb0a-ff0d1c0c4458", "ops_principal": "151fbbe2-0bdb-4837-b4ba-b7c1f00f1293", "agent_principal": "afa8c9a5-7efd-41b4-a652-3b5e18cc44c9", "admin_dsn": "postgresql://postgres:***@127.0.0.1:55499/fkh_rotate_a"}
$ H:\MyWork\Factory-Knowledge-Hub\.venv\Scripts\python.exe -m fkh.tools.instance serve --workdir H:\MyWork\Factory-Knowledge-Hub\.acceptance\rotate-a  → exit=0
  out: {"api": "http://127.0.0.1:8169", "pid": 40968, "ready": true, "instance": "rotate-a"}
$ H:\MyWork\Factory-Knowledge-Hub\.venv\Scripts\python.exe -m fkh.tools.instance stop --workdir H:\MyWork\Factory-Knowledge-Hub\.acceptance\rotate-a  → exit=0
  out: {"stopped": true, "api_was_running": true}
$ H:\MyWork\Factory-Knowledge-Hub\.venv\Scripts\python.exe -m fkh.tools.instance rotate-keys --workdir H:\MyWork\Factory-Knowledge-Hub\.acceptance\rotate-a  → exit=0
  out: {"rotated": true, "jwt_key_id": "jwt-2", "retrieval_key_id": "k-rotate-a-r1", "previous_grace": true, "restart_required": true, "db_app_rotated": true}
$ H:\MyWork\Factory-Knowledge-Hub\.venv\Scripts\python.exe -m fkh.tools.instance serve --workdir H:\MyWork\Factory-Knowledge-Hub\.acceptance\rotate-a  → exit=0
  out: {"api": "http://127.0.0.1:8169", "pid": 18412, "ready": true, "instance": "rotate-a"}
$ H:\MyWork\Factory-Knowledge-Hub\.venv\Scripts\python.exe -m fkh.tools.instance stop --workdir H:\MyWork\Factory-Knowledge-Hub\.acceptance\rotate-a  → exit=0
  out: {"stopped": true, "api_was_running": true}
$ H:\MyWork\Factory-Knowledge-Hub\.venv\Scripts\python.exe -m fkh.tools.instance retire-jwt-previous --workdir H:\MyWork\Factory-Knowledge-Hub\.acceptance\rotate-a  → exit=0
  out: {"retired": true, "restart_required": true}
$ H:\MyWork\Factory-Knowledge-Hub\.venv\Scripts\python.exe -m fkh.tools.instance serve --workdir H:\MyWork\Factory-Knowledge-Hub\.acceptance\rotate-a  → exit=0
  out: {"api": "http://127.0.0.1:8169", "pid": 39184, "ready": true, "instance": "rotate-a"}
$ H:\MyWork\Factory-Knowledge-Hub\.venv\Scripts\python.exe -m fkh.tools.instance stop --workdir H:\MyWork\Factory-Knowledge-Hub\.acceptance\rotate-a  → exit=0
  out: {"stopped": true, "api_was_running": true}
```

## 语义

- `rotate-keys`：旧 JWT 密钥保留为 `jwt.previous.secret`（只验签），新密钥写入 `jwt.secret`；
  DB 应用角色口令立即更新，旧口令失效；要求 API 已停止。
- `retire-jwt-previous`：删除旧 JWT 密钥；下次启动后旧令牌 401。
- 管理员 DB 口令不在本命令轮换。
