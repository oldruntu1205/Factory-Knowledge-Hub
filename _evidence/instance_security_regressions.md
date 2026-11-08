# 客户实例安全回归验收（Windows DACL / restore 边界 / 重启与停止身份 / 冷启动首请求）

- 时间：2026-11-08T17:43:41.713360+00:00
- 基准：7e0f8b1（本轮修改前）
- 本记录不含任何密钥/口令内容；只记录路径、SID、退出码与状态。

## 步骤

| 步骤 | 结果 | 细节 |
|---|---|---|
| secret-dir-acl-private | ✅ | {"path": "H:\\MyWork\\Factory-Knowledge-Hub\\.acceptance\\sec-a\\secrets", "protected": true, "owner_sid": "S-1-5-32-544", "aces": [{"sid": "S-1-5-21-3286290102-2723351879-220780599-500", "allowed": true, "inherited": false, "mask": 2032127}]} |
| secret-file-acl-private | ✅ | {"path": "H:\\MyWork\\Factory-Knowledge-Hub\\.acceptance\\sec-a\\secrets\\jwt.secret", "protected": true, "owner_sid": "S-1-5-32-544", "aces": [{"sid": "S-1-5-21-3286290102-2723351879-220780599-500", "allowed": true, "inherited": false, "mask": 2032127}]} |
| acl-probe-source | ✅ | H:\MyWork\Factory-Knowledge-Hub\.acceptance\acl-probe.json（普通账户：fkh_acl_e03d4572） |
| ordinary-user-cannot-read-secrets | ✅ | {"who": "comfyui\\fkh_acl_e03d4572", "files": {"H:\\MyWork\\Factory-Knowledge-Hub\\.acceptance\\sec-a\\secrets\\db-admin.password": "DENIED:UnauthorizedAccessException", "H:\\MyWork\\Factory-Knowledge-Hub\\.acceptance\\sec-a\\secrets\\jwt.secret": "DENIED:UnauthorizedAccessException", "H:\\MyWork\\Factory-Knowledge-Hub\\.acceptance\\sec-a\\secrets\\db-app.password": "DENIED:UnauthorizedAccessException"}, "dir": "DENIED:UnauthorizedAccessException", "control": "READABLE", "admin_group": false} |
| truncated-dump-still-listable | ✅ | pg_restore --list exit=0（用于制造“清单可读、恢复中途失败”） |
| restore-refuses-nonempty-target | ✅ | exit=1；sentinel 保留 |
| restore-failure-keeps-precreated-empty-dir | ✅ | exit=1；目录仍在且为空 |
| restore-failure-removes-created-dir | ✅ | exit=1；目录已移除 |
| restore-good-instance-serves | ✅ | api=8166；jwt ACL protected=True |
| custom-domain-registered | ✅ | HTTP 201 {'code': 'custom_field', 'label': '客户自定义域', 'outcome': 'EXISTS', 'created': False} |
| restart-after-custom-domain | ✅ | serve_exit=0；domains=['equipment_safety', 'product_catalog', 'quality', 'training', 'custom_field'] |
| stop-refuses-foreign-pid-and-keeps-it | ✅ | exit=1；foreign_alive=True；foreign_port=True |
| cold-start-first-review-passes-without-retry | ✅ | 24 次上传+立即审核，非 200=0；首轮=[200, 200, 200] |

## 命令（退出码）

```text
$ H:\MyWork\Factory-Knowledge-Hub\.venv\Scripts\python.exe -m fkh.tools.instance stop --workdir H:\MyWork\Factory-Knowledge-Hub\.acceptance\sec-a  → exit=0
  out: {"stopped": true, "api_was_running": false}
$ H:\MyWork\Factory-Knowledge-Hub\.venv\Scripts\python.exe -m fkh.tools.instance stop --workdir H:\MyWork\Factory-Knowledge-Hub\.acceptance\sec-restore-good  → exit=0
  out: {"stopped": true, "api_was_running": false}
$ H:\MyWork\Factory-Knowledge-Hub\.venv\Scripts\python.exe -m fkh.tools.instance serve --workdir H:\MyWork\Factory-Knowledge-Hub\.acceptance\sec-a  → exit=0
  out: {"api": "http://127.0.0.1:8165", "pid": 14368, "ready": true, "instance": "sec-a"}
$ H:\MyWork\Factory-Knowledge-Hub\.venv\Scripts\python.exe -m fkh.tools.instance backup --workdir H:\MyWork\Factory-Knowledge-Hub\.acceptance\sec-a --out H:\MyWork\Factory-Knowledge-Hub\.acceptance\sec-a.dump  → exit=0
  out: {"backup": "H:\\MyWork\\Factory-Knowledge-Hub\\.acceptance\\sec-a.dump", "bytes": 212623, "entries": 400}
$ H:\MyWork\Factory-Knowledge-Hub\.venv\Lib\site-packages\pgserver\pginstall\bin\pg_restore.exe --list H:\MyWork\Factory-Knowledge-Hub\.acceptance\sec-truncated.dump  → exit=0
  out: 3231; 0 16881 ROW SECURITY public training_practical_signoffs postgres
  out: 3229; 0 16846 ROW SECURITY public training_questions postgres
  out: 3225; 0 16782 ROW SECURITY public training_roles postgres
$ H:\MyWork\Factory-Knowledge-Hub\.venv\Scripts\python.exe -m fkh.tools.instance restore --from H:\MyWork\Factory-Knowledge-Hub\.acceptance\sec-a.dump --workdir H:\MyWork\Factory-Knowledge-Hub\.acceptance\sec-restore-nonempty --db-port 55496 --api-port 8166 --db-name fkh_sec_r_nonempty --name r-nonempty  → exit=1
  err: 目标目录非空：H:\MyWork\Factory-Knowledge-Hub\.acceptance\sec-restore-nonempty；restore 只接受全新目录（不存在或为空目录）
$ H:\MyWork\Factory-Knowledge-Hub\.venv\Scripts\python.exe -m fkh.tools.instance restore --from H:\MyWork\Factory-Knowledge-Hub\.acceptance\sec-truncated.dump --workdir H:\MyWork\Factory-Knowledge-Hub\.acceptance\sec-restore-empty --db-port 55496 --api-port 8166 --db-name fkh_sec_r_empty --name r-empty  → exit=1
  err: RuntimeError: pg_restore 失败：
  err: 
  err: pg_restore: error: could not read from input file: end of file
$ H:\MyWork\Factory-Knowledge-Hub\.venv\Scripts\python.exe -m fkh.tools.instance restore --from H:\MyWork\Factory-Knowledge-Hub\.acceptance\sec-truncated.dump --workdir H:\MyWork\Factory-Knowledge-Hub\.acceptance\sec-restore-fresh --db-port 55496 --api-port 8166 --db-name fkh_sec_r_fresh --name r-fresh  → exit=1
  err: RuntimeError: pg_restore 失败：
  err: 
  err: pg_restore: error: could not read from input file: end of file
$ H:\MyWork\Factory-Knowledge-Hub\.venv\Scripts\python.exe -m fkh.tools.instance restore --from H:\MyWork\Factory-Knowledge-Hub\.acceptance\sec-a.dump --workdir H:\MyWork\Factory-Knowledge-Hub\.acceptance\sec-restore-good --db-port 55496 --api-port 8166 --db-name fkh_sec_r_good --name r-good  → exit=0
  out: {"schema": 1, "name": "r-good", "workdir": "H:\\MyWork\\Factory-Knowledge-Hub\\.acceptance\\sec-restore-good", "pgdata": "H:\\MyWork\\Factory-Knowledge-Hub\\.acceptance\\sec-restore-good\\pgdata", "db_port": 55496, "db_name": "fkh_sec_r_good", "admin_user": "postgres", "app_role": "fkh_app_sec_r_good", "api_port": 8166, "secret_dir": "secrets", "domains": ["custom_field", "equipment_safety", "product_catalog", "quality", "training"], "created_at": "2026-11-08T17:43:14.545718+00:00", "restored_from": "H:\\MyWork\\Factory-Knowledge-Hub\\.acceptance\\sec-a.dump", "tenant_id": "1730e5ff-93f7-47c9-91d8-11023cdafb4a", "ops_principal": "092593d8-a268-4e88-9bd9-98838d7fd75c", "agent_principal": "418803e2-fa35-436f-bce6-1e3558766660", "admin_dsn": "postgresql://postgres:***@127.0.0.1:55496/fkh_sec_r_good"}
$ H:\MyWork\Factory-Knowledge-Hub\.venv\Scripts\python.exe -m fkh.tools.instance serve --workdir H:\MyWork\Factory-Knowledge-Hub\.acceptance\sec-restore-good  → exit=0
  out: {"api": "http://127.0.0.1:8166", "pid": 38688, "ready": true, "instance": "r-good"}
$ H:\MyWork\Factory-Knowledge-Hub\.venv\Scripts\python.exe -m fkh.tools.instance stop --workdir H:\MyWork\Factory-Knowledge-Hub\.acceptance\sec-restore-good  → exit=0
  out: {"stopped": true, "api_was_running": true}
$ H:\MyWork\Factory-Knowledge-Hub\.venv\Scripts\python.exe -m fkh.tools.instance stop --workdir H:\MyWork\Factory-Knowledge-Hub\.acceptance\sec-a  → exit=0
  out: {"stopped": true, "api_was_running": true}
$ H:\MyWork\Factory-Knowledge-Hub\.venv\Scripts\python.exe -m fkh.tools.instance serve --workdir H:\MyWork\Factory-Knowledge-Hub\.acceptance\sec-a  → exit=0
  out: {"api": "http://127.0.0.1:8165", "pid": 27424, "ready": true, "instance": "sec-a"}
$ H:\MyWork\Factory-Knowledge-Hub\.venv\Scripts\python.exe -m fkh.tools.instance stop --workdir H:\MyWork\Factory-Knowledge-Hub\.acceptance\sec-a  → exit=1
  err: pid=10400 不是本实例的 API 进程（H:/MyWork/Factory-Knowledge-Hub/.venv/Scripts/python.exe -m uvicorn foreign_app:app --host 127.0.0.1 --port 8199），拒绝停止
$ H:\MyWork\Factory-Knowledge-Hub\.venv\Scripts\python.exe -m fkh.tools.instance stop --workdir H:\MyWork\Factory-Knowledge-Hub\.acceptance\sec-a  → exit=0
  out: {"stopped": true, "api_was_running": true}
$ H:\MyWork\Factory-Knowledge-Hub\.venv\Scripts\python.exe -m fkh.tools.instance serve --workdir H:\MyWork\Factory-Knowledge-Hub\.acceptance\sec-a  → exit=0
  out: {"api": "http://127.0.0.1:8165", "pid": 15632, "ready": true, "instance": "sec-a"}
$ H:\MyWork\Factory-Knowledge-Hub\.venv\Scripts\python.exe -m fkh.tools.instance stop --workdir H:\MyWork\Factory-Knowledge-Hub\.acceptance\sec-a  → exit=0
  out: {"stopped": true, "api_was_running": true}
$ H:\MyWork\Factory-Knowledge-Hub\.venv\Scripts\python.exe -m fkh.tools.instance serve --workdir H:\MyWork\Factory-Knowledge-Hub\.acceptance\sec-a  → exit=0
  out: {"api": "http://127.0.0.1:8165", "pid": 37580, "ready": true, "instance": "sec-a"}
$ H:\MyWork\Factory-Knowledge-Hub\.venv\Scripts\python.exe -m fkh.tools.instance stop --workdir H:\MyWork\Factory-Knowledge-Hub\.acceptance\sec-a  → exit=0
  out: {"stopped": true, "api_was_running": true}
$ H:\MyWork\Factory-Knowledge-Hub\.venv\Scripts\python.exe -m fkh.tools.instance serve --workdir H:\MyWork\Factory-Knowledge-Hub\.acceptance\sec-a  → exit=0
  out: {"api": "http://127.0.0.1:8165", "pid": 8164, "ready": true, "instance": "sec-a"}
$ H:\MyWork\Factory-Knowledge-Hub\.venv\Scripts\python.exe -m fkh.tools.instance stop --workdir H:\MyWork\Factory-Knowledge-Hub\.acceptance\sec-a  → exit=0
  out: {"stopped": true, "api_was_running": true}
```

## 剩余边界

- Windows DACL 由创建实例的账户持有；换账户运行 serve 需先以目标账户重新 provision/restore。
- restore 的失败清理只覆盖本工具创建的资源；外部手工创建的库/角色不在清理范围。
- 冷启动回归覆盖 3 次重启 × 每轮首轮及后续请求，未做长稳压测。
