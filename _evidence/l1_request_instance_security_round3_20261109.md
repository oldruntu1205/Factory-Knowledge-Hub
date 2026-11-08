# L1 复送（第 3 轮）：M2 的"DDL 结果不确定"路径

- 日期：2026-11-09
- 上一轮回执：`_evidence/l1_reply_instance_security_followup_20261109.md`（1 个 MUST_FIX）
- 沙箱：`read-only`；**不要探索仓库**，只基于本文事实与片段判断。
- 预算：本切片第 3 次 L1 调用（上限 3 次）。

## 一、MUST_FIX 的修复

### 1) 发出创建前记录 attempted；结果不确定只报告、不按名字删除

`command_restore` 现在：

```python
db_creation_attempted = False
role_creation_attempted = False
...
if _database_exists(root_dsn, args.db_name):
    raise SystemExit("...已存在...")
db_creation_attempted = True
try:
    _create_database(root_dsn, args.db_name, on_created=_mark_db_created)
except SystemExit:
    db_creation_attempted = False   # 只可能是"已存在"（非本次创建）
    raise
if _role_exists(root_dsn, role):
    raise SystemExit("...已存在...")
role_creation_attempted = True
try:
    _ensure_business_role(..., on_created=_mark_role_created, require_new=True)
except SystemExit:
    role_creation_attempted = False  # 只可能是"已存在"（竞态）
    raise
```

`_cleanup_restore` 增加：

```python
if db_creation_attempted and not created_db:
    failures.append(
        f"database {db_name} 创建结果未确认（可能已创建）；未自动删除，请人工核查"
    )
if role_creation_attempted and not created_role:
    failures.append(
        f"role {role} 创建结果未确认（可能已创建）；未自动删除，请人工核查"
    )
```

- `on_created` 在 `_exec(CREATE ...)` 正常返回后立即置 `created_*=True`；
- 若 `_exec` 抛异常（连接中断、服务端已提交但客户端没拿到结果），`created_*` 仍为 false，
  清理回执与原异常 note 会明确报告"可能遗留 + 人工核查"，**不会**按名字 drop；
- 若 `_create_database`/`_ensure_business_role` 因"已存在"抛 `SystemExit`，attempted 复位为
  false，不会误报。

### 2) 既有角色不会被修改（回应 SHOULD_FIX）

`_ensure_business_role(..., require_new=True)` 默认：

```python
created = existing is None
if not created and require_new:
    raise SystemExit(f"角色 {role} 已存在；...拒绝修改既有角色")
if created:
    _exec(CREATE ROLE); on_created()
ALTER ROLE ... PASSWORD ...   # 只有 require_new 分支通过后才执行
```

restore 在调用前还有 `_role_exists` 预检；竞态由 `require_new` 分支兜底，任何已存在角色都会
直接失败，不会进入 `ALTER ROLE`/GRANT。

### 3) workdir 前置条件的确认（回应 QUESTIONS）

- "目标目录不存在或为空"指**整个 workdir**：`command_restore` 在创建任何东西之前检查
  `workdir.exists()` 且 `any(iterdir())`，非空即拒绝；因此其下 `pgdata`/`secrets` 不可能预先存在。
- `workdir_created`、`created_pgdata`、`created_secrets` 都在**第一次创建动作之前**登记；
  `instance_written` 在 `_write_instance` 之前登记；`created_db`/`created_role` 由 CREATE 成功
  后的立即回调登记；attempted 标记在发出 CREATE 前登记。

## 二、新增测试

- `tests/unit/test_restore_cleanup.py::test_cleanup_reports_unconfirmed_creations_without_dropping`：
  `created_db=False`/`created_role=False` + attempted=True + root_dsn 可用 → 断言清理**没有**
  发出任何 `_exec`（不按名字删除），且失败回执包含两个"创建结果未确认 / 请人工核查"。
- 同文件先前的用例继续覆盖：drop database / stop postgres / remove pgdata 同时失败时，
  drop role / remove secrets / 删 instance.json 仍被执行；原异常不被清理失败覆盖。
- `tests/e2e/test_commit_boundary.py`：正常 return 409 的写入必须回滚（上一轮 M5 证据）。
- 全量：`uv run pytest -q` → **682 passed**；`ruff`/`mypy` 全绿。
- `scripts/acceptance_instance_security_regressions.py` → 13 步全 ✅（24 次冷启动立即审核全 200）；
  `scripts/acceptance_instance_lifecycle.py` → 25 步全 ✅（新装/重启/撤销/备份/恢复/审计）。

## 三、请 L1 判断

1. attempted + "不确定只报告、不按名字删除" 是否满足 M2？是否还需要在回执里给出具体的
   人工核查 SQL/命令（本轮只要求最小修复）？
2. `require_new=True` + `_role_exists` 预检是否足以保证既有角色不被修改？
3. 以上是否可给 PASS？若仍需 MUST_FIX，请只列最小阻塞项。

## 四、固定回复骨架

```text
VERDICT: PASS | CHANGES_REQUESTED

MUST_FIX:
- <问题>: <为什么阻塞> -> <最小修复>

SHOULD_FIX:
- <问题>: <建议>

BOUNDARY:
- <本轮不要求覆盖的项>
```

## 五、边界

- 只审上述四件事；混合检索、密钥轮换、`instance_id` 不在本轮。
- 不要探索仓库；如需片段请点名。
- 本轮 L1 只判断，不要求跑测试或改代码。
