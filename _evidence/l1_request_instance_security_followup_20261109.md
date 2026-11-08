# L1 复送：实例交付安全回归（第 2 轮，回应 CHANGES_REQUESTED）

- 日期：2026-11-09
- 上一轮回执：`_evidence/l1_reply_instance_security_20261109.md`（CHANGES_REQUESTED，5 个 MUST_FIX）
- 沙箱：`read-only`；**不要探索仓库**，只基于本文事实与片段判断。
- 预算：本切片第 2 次 L1 调用（上限 3 次）。

## 一、逐条回应 MUST_FIX

### M1. restore 清理可能中途退出 / 覆盖原始异常 — 已修

新增 `_cleanup_restore(...)`：每个资源独立 `attempt`；只把 `FileNotFoundError` 当正常；
其它失败收集为字符串，**不 raise、不中断后续资源**。调用方逐条打印并 `exc.add_note(...)`，
然后 `raise` 原始异常。资源包括：drop database（仅本次 created_db）、drop role（仅本次
created_role）、stop postgres、remove pgdata（仅本次 created_pgdata）、remove secrets
（仅本次 created_secrets）、remove instance.json（仅本次 instance_written）、remove workdir
（仅本次 workdir_created，且只在已空时 rmdir；非空则报告保留）。

新增 `tests/unit/test_restore_cleanup.py`：让 drop database / stop postgres / remove pgdata
同时失败，并让 workdir 留有 residual 文件；断言 4 条失败都被收集，且 drop role、remove
secrets、删 instance.json **仍被执行**；另一用例断言缺失资源不产生失败。

### M2. 资源创建与归属标记之间的空窗 — 已修

- `_create_database(..., on_created=...)` 在 `CREATE DATABASE` 成功后**立即回调**；
- `_ensure_business_role(..., on_created=...)` 在 `CREATE ROLE` 成功后**立即回调**，
  早于后续 `ALTER ROLE`/GRANT；
- restore 前先 `_role_exists(root_dsn, role)`，角色已存在则拒绝，避免改别的实例的口令；
- `instance_written = True` 放在 `_write_instance` 之前，半写的 instance.json 也会被清；
- `pgdata`/`secrets` 的"本次创建"由前置"目标目录不存在或为空"保证（这两者不可能预先存在）。

### M3. nonce 不匹配仍可能判 ours — 已修（并补旧格式的实例归属证据）

当前实际实现（不是上一轮片段里的简化版）：

```python
if nonce:
    try: env_nonce = proc.environ().get("FKH_INSTANCE_PID_NONCE")
    except (psutil.AccessDenied, OSError):
        return _ProcessVerdict(pid, "denied", "无权限读取进程环境（nonce 不可证）")
    if env_nonce != nonce or not is_this_app:
        return _ProcessVerdict(pid, "foreign", cmdline)
    if _env_matches_instance(proc, db_port=db_port, db_name=db_name) is not True:
        return _ProcessVerdict(pid, "foreign", cmdline)
    return _ProcessVerdict(pid, "ours", cmdline, create_time)
```

- 新格式（JSON 有 nonce）：环境不可读 → `denied`；nonce 不匹配 → `foreign`；**不再回退**到
  "同 app + 同端口"。
- 旧格式（纯整数 pid）：除 app + `--port <api_port>` + 端口监听者匹配（或启动器父子关系）外，
  还要求子进程环境里的 `FKH_DATABASE_URL` 的**端口与库名**匹配本实例；环境不可读 → `denied`。
- pid 文件 JSON 里的 `workdir` 与当前 workdir 不一致 → 直接拒绝停止。

新增单测：nonce mismatch → foreign；env 不可读 → denied；`FKH_DATABASE_URL` 指向别的库 → foreign。

### M4. 终止对象与 absent 分支 — 已修

- `_classify_api_process` 返回 `_ProcessVerdict(pid, state, cmdline, create_time)`；
- `_terminate_process(verdict)` 在 terminate 前**重新读取 `create_time`**，与判定时不一致即
  `SystemExit("已被替换...")`，不杀新进程；foreign/denied 的 verdict 直接拒绝；
- stop 终止顺序：监听者 → pid 文件进程 → 启动器，按 pid dedup；每个目标都是独立 verdict；
- `absent` 分支：pid 文件指向的进程不存在时，仍检查 `port_owner(api_port)`：
  - 监听者是 ours → 作为目标终止（pid 文件丢失也能自愈）；
  - 监听者 foreign/denied → 拒绝停止，不盲目停 PG；
  - 无监听者 → 才继续停 PG。
- 启动器只作附加清理项：若其 pid 已被回收/复用（foreign），不终止它，但监听者已确认 ours，
  不影响停止。

新增单测：foreign verdict 拒绝；create_time 变化拒绝；absent + 无监听者停 PG；监听者优先于启动器。

### M5. 错误状态码不等于事务异常退出 — 已修

`CommitBeforeSend` 现在对 `status >= 400` **显式 rollback**，不依赖"端点一定抛异常"：

```python
if conn is not None:
    if status < 400:
        await run_in_threadpool(_commit_before_send, conn)
    else:
        await run_in_threadpool(_rollback_before_send, conn)
```

新增 `tests/e2e/test_commit_boundary.py`：临时路由在 `get_conn` 连接上 INSERT 一行，然后
**正常 return** `JSONResponse(status_code=409)`（不是 raise）；请求后以管理员查询，断言该行
不存在。另补单测：403 响应 rollback 被调用，commit 未调用。

## 二、SHOULD_FIX 的处理

- **DACL 所有者**：`AclInfo` 增加 `owner_sid`；`assert_private`/`harden` 要求 owner 为当前账户
  或 `BUILTIN\Administrators`（管理员创建文件的 owner 常是组）；其它 owner 拒绝并报
  owner/当前 SID。`harden` 在改 DACL 前先校验 owner。
- **reparse point**：`tighten`/`harden` 拒绝 symlink/junction。
- **ACL API 错误**：`GetNamedSecurityInfoW` 同时请求 `OWNER_SECURITY_INFORMATION`；
  `SetNamedSecurityInfoW` 返回码非 0 即抛错；错误含路径、运行 SID、owner SID、操作。
- **serve 自愈**：`serve` 先 `harden_secrets`，失败信息包含路径与 SID，并指向
  `harden-secrets`。
- **提交边界**：本项目数据库写入均在端点的同步调用内完成；无流式生成器/后台任务在
  响应后继续写同一连接（后台 worker 用自己的连接/事务）。没有"正常返回 4xx 但故意保留写入"
  的端点；e2e 用例覆盖了这种写法会被回滚。
- **实例身份**：`record.domains ⊆ 实际域` 只解决"追加自定义域后重启"；"复制 secrets + 同租户"
  的另一实例仍可能通过，这是本轮**已知边界**，未引入 `instance_id`（需要迁移 + 初始化 + 恢复
  改动，超出本轮四件事）。已在 docs 的边界中写明。

## 三、验证结果

- `uv run pytest -q`：**681 passed**（原 672 + 新增 e2e 1 + 单测 8；含新增的 commit-boundary）。
- `uv run ruff check .`：All checks passed。
- `uv run mypy`：Success: no issues found in 87 source files。
- `scripts/acceptance_instance_security_regressions.py --reuse --acl-probe-json ...`：
  13 个步骤全 ✅；24 次冷启动上传+立即审核非 200=0；普通账户读三个密钥与目录全部 DENIED、
  控制文件 READABLE、`admin_group=false`；restore 非空/空目录/全新目录/正常恢复全部符合预期；
  自定义域重启成功；外部 uvicorn pid 被拒绝停止且仍存活。
- `scripts/acceptance_instance_lifecycle.py`：25 步全 ✅（新装/重启/撤销/备份/恢复/审计）。

## 四、请 L1 判断

1. M1/M2 的清理与归属登记现在是否闭合？是否还有"内部创建成功后、调用方尚不知情"的失败点？
   `_role_exists` 预检 + `on_created` 回调是否足够？
2. M3 的新格式 nonce 严格化 + 旧格式 `FKH_DATABASE_URL` 绑定是否足够？旧格式仍需
   "app + 端口 + 监听者"是否残留可被同 app 异实例利用的窗口？
3. M4 的 create_time 复核与 absent 分支是否闭合？启动器 foreign 时"忽略并继续停已确认的
   监听者"是否可接受，还是必须整体拒绝？
4. M5 的"status >= 400 显式 rollback"是否可接受？是否存在应保留写入的 4xx 语义？
5. 剩余的 SHOULD_FIX（尤其 `instance_id` 与 DACL 管理员 owner）是否可留到下一轮，还是
   必须在提交前补？请给出 PASS / CHANGES_REQUESTED 与最小 MUST_FIX。

## 五、固定回复骨架

```text
VERDICT: PASS | CHANGES_REQUESTED

MUST_FIX:
- <问题>: <为什么阻塞> -> <最小修复>

SHOULD_FIX:
- <问题>: <建议>

QUESTIONS:
- <需补充的事实/证据>

BOUNDARY:
- <本轮不要求覆盖的项>
```

## 六、边界

- 只审上述四件事；混合检索、密钥轮换不在本轮。
- 不要探索仓库；如需片段请点名。
- 本轮 L1 只判断，不要求跑测试或改代码。
