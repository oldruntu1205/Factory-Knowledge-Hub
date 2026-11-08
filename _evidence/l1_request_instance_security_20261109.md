# L1 送审：实例交付安全回归（DACL / restore 清理 / 启停身份 / 响应前提交）

- 日期：2026-11-09
- 基准：`7e0f8b1`（本轮修改前；工作树中为本轮 diff）
- 沙箱：`read-only`；**不要探索仓库**，只基于本文事实与片段判断。
- 预算：本切片 L1 调用 1 次；只回答下面的问题，不要扩展到混合检索/密钥轮换。

## 一、本轮要解决的四件事与实测证据

### 1) Windows 密钥 DACL + 普通账户读拒绝

- 旧实现只有 `os.chmod(0o600)`；Windows 上它只改只读属性，不改 DACL，默认继承的
  `BUILTIN\Users:(RX)` 仍可读。
- 新实现 `src/fkh/tools/winacl.py`：用 `SetNamedSecurityInfoW` 重建 DACL 为
  **PROTECTED + 单条允许 ACE（当前账户 SID，FILE_ALL_ACCESS）**；目录 ACE 带
  `OBJECT_INHERIT_ACE|CONTAINER_INHERIT_ACE`。`assert_private` 读取 DACL，要求
  `protected=True`、存在当前账户允许 ACE、**不存在任何其它允许 SID**（Everyone /
  Authenticated Users / Users / INTERACTIVE / NETWORK 都算失败）。
- `_write_secret` 先收紧 `secrets/` 目录、再创建文件、再收紧文件，避免"写完再补救"的窗口。
- `initdb` 的临时口令文件从 `%TEMP%` 根目录改为**私有临时目录**（先收紧目录再创建文件）。
- `serve` 启动前调用 `harden_secrets()` 自愈旧实例；新增 `harden-secrets` 运维命令。
- 非 Windows 退化为 `chmod 0700/0600`（Linux 交付由部署规范做系统账户隔离）。

实测（管理员 PowerShell 创建临时**普通**账户 `fkh_acl_*`，用该账户的 PowerShell 读文件）：

```json
{"who":"comfyui\\fkh_acl_e03d4572",
 "files":{"...\\db-admin.password":"DENIED:UnauthorizedAccessException",
          "...\\jwt.secret":"DENIED:UnauthorizedAccessException",
          "...\\db-app.password":"DENIED:UnauthorizedAccessException"},
 "dir":"DENIED:UnauthorizedAccessException","control":"READABLE","admin_group":false}
```

DACL 取证（无密钥内容）：`protected=true`，`aces=[{sid: S-1-5-21-...-500, allowed:true,
inherited:false, mask:2032127}]`。

### 2) restore 只接受全新目录，失败只清理本次创建的资源

旧行为：目标只有 `pg_restore --list` 失败才在创建前退出；一旦进入创建阶段，失败时
`shutil.rmtree(workdir)` **整目录删除**，会连带删掉调用方预建的文件/目录；且不删本次创建的
业务角色。

新行为：
- 前置：目标必须是**不存在或空目录**；非空直接拒绝，不创建任何东西。
- 失败清理：只 drop 本次 `created_db`、drop 本次 `created_role`、停并删本次 `pgdata`、
  删本次 `secrets/` 与 `instance.json`；`workdir_created` 为假时只清内容、保留调用方预建空目录。
- 成功路径不变：单事务 `pg_restore`、新口令/新 JWT 密钥、业务角色非 owner。

实测：
- 非空目标（含 `sentinel.txt`）→ exit 1，sentinel 保留，无 `instance.json`/`secrets`/`pgdata`；
- 预建空目录 + 截断 dump（`pg_restore --list` 仍 exit 0、恢复中途 EOF）→ exit 1，目录仍存在且为空；
- 全新目录 + 同一截断 dump → exit 1，目录被移除，端口未监听；
- 正常 dump → 恢复成功，`serve` 成功，`secrets` DACL protected。

### 3) 两个回归

**(a) 自定义域登记后仍能重启。** 旧 `_identity_probe` 用 `set(codes) == set(record.domains)`
全等比较；运行时 `POST /v1/config/domains` 追加自定义域后，重启时数据库域集合变大 → 被误判
"应答者不是本实例" → `serve` 失败。改为**子集**判定 `record.domains ⊆ codes`；身份仍由
"本实例密钥签发的令牌 + 该租户数据可见"证明（换实例必 401）。实测：注册 `custom_field`
→ stop → serve exit 0 → 域清单含 `custom_field`。

**(b) pid 文件指向其他服务。** 旧 `_pid_matches` 是「cmdline 含任一 needle 即本实例」
（`uvicorn` 单独命中），会把另一个 uvicorn 服务杀掉；且 `.venv\Scripts\python.exe` 是启动器，
真正监听的是其子进程，旧 pid 文件记录启动器会导致"停了启动器、服务还在"。
新行为：pid 文件 JSON `{pid(监听者), launcher_pid, nonce, api_port, workdir}`；非ce 同时注入
子进程环境。`stop` 判定：
- nonce 命中且 cmdline 是本 app → ours；
- 或 cmdline 是本 app + `--port <api_port>` + 该端口监听者就是它，或监听者是它的子进程（启动器场景）；
- 其它 → foreign/denied，**raise 且不 terminate、不 stop PG**；
- 进程不存在 → absent，视为陈旧 pid，继续停 PG。
终止顺序：监听者 → pid 文件进程 → 启动器，逐个 dedup。

实测：起一个真正的外部 uvicorn（`foreign_app:app`，端口 8199），把其 pid 写进 A 的
`api.pid`；`stop A` → exit 1，外部进程仍存活且 8199 仍 LISTEN，A 自己的 API 仍在 8165 响应。

### 4) 冷启动 review 偶发 403（文档已记录，本轮 RCA）

**不是** 连接池/租户绑定预热。实测复现（修复前）：
- `POST /v1/knowledge/documents` → 201；
- 3.9ms 后 `POST /v1/knowledge/{id}/review` → 403（`VersionNotFound` → ACCESS_DENIED）；
- 轮询数据库：该版本行在 **响应后 158ms** 才可见；
- 立即重试 → 200。
根因：FastAPI 0.162 的 `request_response` 先 `await response(scope, receive, send)`，**之后**才关闭
request exit stack；`get_conn` 的 yield 依赖在 exit stack 里提交事务。客户端收到 201 时事务还没提交，
紧随其后的请求在另一连接上看不到该行。

修复：`src/fkh/api/db.py` 新增 `CommitBeforeSend` ASGI 中间件，包在路由外；在第一条
`http.response.start` **转发前**提交 `request.state.conn`，仅 `status < 400`；错误响应不提前提交，
仍由依赖退出回滚；commit 失败在响应头发出前抛出（客户端不会收到成功）。`get_conn` 把连接挂到
`request.state.conn`。中间件注册在 FastAPI 创建后、BaseHTTPMiddleware 之前，使其紧贴路由。

实测（修复后）：3 次冷启动 × 每轮 8 次「上传 + 立即审核」，共 24 次，**非 200 = 0**，
每轮首个 review 均 200；验收脚本不再重试。全量 `uv run pytest -q`：**672 passed**；
`ruff` / `mypy` 全绿。

## 二、关键代码片段

### A. CommitBeforeSend（`src/fkh/api/db.py`）

```python
class CommitBeforeSend:
    def __init__(self, app): self.app = app
    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send); return
        state = scope.setdefault("state", {})
        committed = False
        async def send_wrapper(message):
            nonlocal committed
            if not committed and message["type"] == "http.response.start":
                committed = True
                conn = state.get("conn")
                status = int(message.get("status", 500))
                if conn is not None and status < 400:
                    await run_in_threadpool(_commit_before_send, conn)
            await send(message)
        await self.app(scope, receive, send_wrapper)

def get_conn(request, context):
    ...
    with tenant_connection(pool, context.tenant_id) as conn:
        request.state.conn = conn
        yield conn
```

### B. restore 失败清理（`src/fkh/tools/instance.py`）

```python
except BaseException:
    if root_dsn is not None:
        if created_db: _exec(root_dsn, f'DROP DATABASE IF EXISTS "{args.db_name}" WITH (FORCE)')
        if created_role: _exec(root_dsn, f'DROP ROLE IF EXISTS "{role}"')
    if pgdata.exists(): stop_postgres(pgdata)
    if pgdata.exists(): shutil.rmtree(pgdata)
    shutil.rmtree(workdir / "secrets")
    instance_path(workdir).unlink(missing_ok=True)
    if workdir_created: workdir.rmdir()   # 只删本工具创建且已空的目录
    raise
```

### C. 进程身份判定（`src/fkh/tools/instance.py`）

```python
def _classify_api_process(pid, *, api_port, nonce=None):
    # absent / denied / foreign / ours
    if nonce and env_nonce == nonce and is_this_app: return "ours", cmdline
    if not (is_this_app and _args_have_port(args, api_port)): return "foreign", cmdline
    owner = port_owner(api_port)
    if owner is None: return "foreign", ...
    if owner["pid"] == pid: return "ours", cmdline
    # 启动器场景：监听者必须是本 app，且 pid 是监听者的父进程
    if _is_this_app(listener_args) and pid in {p.pid for p in psutil.Process(owner["pid"]).parents()}:
        return "ours", cmdline
    return "foreign", ...
```

### D. DACL 重建（`src/fkh/tools/winacl.py`）

```python
# 单条 ACE：当前账户 FILE_ALL_ACCESS；目录带 OI|CI
ace_flags = OBJECT_INHERIT_ACE | CONTAINER_INHERIT_ACE if directory else 0
AddAccessAllowedAceEx(acl, ACL_REVISION, ace_flags, FILE_ALL_ACCESS, sid)
SetNamedSecurityInfoW(path, SE_FILE_OBJECT,
    DACL_SECURITY_INFORMATION | PROTECTED_DACL_SECURITY_INFORMATION, None, None, acl, None)
# assert_private: protected=True；允许 ACE 只能有当前账户 SID
```

## 三、请 L1 判断的问题

1. **CommitBeforeSend 的事务语义**：以 `http.response.start` 且 `status < 400` 作为"提交成功事务"
   的判据，是否覆盖本项目所有端点？错误响应是否仍可靠回滚（依赖 exit stack）？与
   BaseHTTPMiddleware 的缓冲/流式响应、客户端断开、commit 失败（抛在 send 前）是否存在遗漏？
   `status < 400` 会不会把某些"正常返回但业务失败"的响应误提交？
2. **DACL 设计**：只授予当前账户、不授予 SYSTEM/Administrators，是否可接受？Windows 上
   文件/目录/父目录继承、低完整性/沙箱进程、symlink/junction、非 NTFS 等边界是否有致命遗漏？
   `assert_private` 只检查"其它允许 ACE"是否足够（是否还要检查 deny ACE / 所有者 / 完整性标签）？
3. **restore 清理**：`created_db`/`created_role`/`workdir_created` 三个标记是否覆盖所有失败点
   （`ensure_postgres` 中途失败、`_create_database` 存在性拒绝、`_ensure_business_role`
   中途失败、`pg_restore` 中途失败、`_assert_auth_isolation` 失败）？"只接受不存在或空目录"
   是否过严/过松？drop role 在 drop db 之后是否足够（角色可能持有其它库对象）？
4. **启停身份**：nonce + 监听者/启动器 + 端口所有者组合是否足以避免"杀错服务"与"留孤儿"
   （venv 启动器 → 子进程、多级包装、PID 复用）？absent pid 时继续停 PG 是否安全？
   外部服务占着本实例端口时，stop 的行为是否应更明确（当前会拒绝并建议人工处理）？
5. **自定义域重启**：`record.domains ⊆ 实际域` 是否足够证明"应答者是本实例"？在"同机复制了
   secrets、另一个实例用同租户"的威胁模型下，是否还需要额外的实例指纹（如 DB 文件名/实例 ID）？
   若需要，最小改动是什么？
6. **交付边界**：`serve` 启动时自动 `tighten` 旧密钥 ACL（而非直接拒绝）是否可接受？如果
   运行账户不是密钥所有者，`tighten` 会失败并拒绝启动——这个失败信息是否足够可运维？还有哪些
   文档/测试缺口必须在合并前补？

## 四、固定回复骨架

```text
VERDICT: PASS | CHANGES_REQUESTED

MUST_FIX:
- <问题>: <为什么是阻塞项> -> <最小修复>

SHOULD_FIX:
- <问题>: <建议>

QUESTIONS:
- <需要实现方补充的事实/证据>

BOUNDARY:
- <本轮不要求覆盖的项>
```

## 五、边界声明

- 本文只覆盖上述四件事；**混合检索、密钥轮换不在本轮**，请不要展开。
- 不要探索仓库；如需某个文件片段，请在 QUESTIONS 里点名，由实现方补充。
- 本轮 L1 只做判断，不要求你跑测试或改代码。
