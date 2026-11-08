# L1 复送：密钥轮换（回应 CHANGES_REQUESTED）

- 日期：2026-11-09
- 上一轮回执：`_evidence/l1_reply_instance_rotation_20261109.md`
- 沙箱：`read-only`；不要探索仓库；只基于本文事实与片段判断。
- 预算：本切片第 2 次 L1 调用（上限 3 次）。

## 一、MUST_FIX：DB "结果不确定"语义

### 1) 显式事务 + 异常分类

`ALTER ROLE` 不再用 autocommit；在显式事务里执行，只有 `commit()` 成功才算确认：

```python
def _alter_role_password(root_dsn, role, password):
    try:
        with psycopg.connect(root_dsn, autocommit=False) as conn:
            conn.execute(f'ALTER ROLE "{role}" PASSWORD {_sql_literal(password)}')
            conn.commit()
    except (psycopg.OperationalError, psycopg.InterfaceError) as exc:
        # 连接中断可能发生在服务端提交之后 → 不确定
        raise _PasswordChangeUncertain(str(exc)) from exc
    except psycopg.errors.DatabaseError as exc:
        # 服务端明确拒绝，事务未提交 → 旧口令仍有效
        raise SystemExit(f"ALTER ROLE 被 PostgreSQL 拒绝：{exc}") from exc
```

注意：`psycopg.OperationalError` 是 `DatabaseError` 的子类，所以**先**捕获连接类异常，
再捕获一般 `DatabaseError`；否则连接中断会被误判成"服务端拒绝"。

### 2) 不确定时保留 `.new`、报告、不假装旧口令有效

```python
try:
    _alter_role_password(root_dsn, record["app_role"], new_app)
except _PasswordChangeUncertain as exc:
    record_event("db_app_rotated_unconfirmed",
                 pending_file=str(tmp_app), postgres_left_running=True)
    _update_record(workdir, record, rotation_history=history)
    raise SystemExit(
        f"DB 口令变更结果待确认（连接中断，可能已生效）：{exc}；"
        f"新口令保存在 {tmp_app}（私有 ACL）。请先分别用旧/新口令测试登录，"
        "确认后再替换 db-app.password；pending 文件存在期间 rotate/serve 会拒绝继续"
    ) from exc
```

- 不确定路径**不删除** `.new`、**不停 PG**（留给人工分别用旧/新口令测试）；
- 报告给出文件路径与操作顺序，不输出口令；
- `rotation_history` 记录 `db_app_rotated_unconfirmed`（不含密钥）。

### 3) `.new` 存在时的处理

新增：

```python
PENDING_SECRET_SUFFIXES = ("jwt.secret.new", "jwt.previous.secret.new", "db-app.password.new")

def _assert_no_pending_secret_files(workdir):
    pending = [...]
    if pending: raise SystemExit("检测到待恢复的临时密钥文件：...；先确认当前生效的密钥/口令...")
```

`serve` / `rotate-keys` / `retire-jwt-previous` 启动时都调用；**不覆盖**待恢复文件。
轮换/退役都要求 API 已停止，因此三者是串行的，不会并发改密钥。

### 4) 原子写失败也保留 `.new`

`_atomic_write_secret` 先 `_write_secret(.new)`（私有 ACL），再 `os.replace`；
替换失败时保留 `.new` 并报错。单测固定了这一点。
`_write_instance` 也改成临时文件 + `os.replace`；metadata 不含密钥。

## 二、SHOULD_FIX 的处理

- **多密钥/kid**：维持 current+previous、固定 HS256、固定 claims；`jwt_previous_secrets`
  增加 `max_length=2`，`decode_token` 去重并最多尝试 3 个。未引入 `kid`（本轮边界）。
- **宽限期**：单把 previous、默认拒绝二次轮换、`--force` 明确提示会让更早令牌失效；
  `retire` 要求停服。
- **部分成功**：JWT、DB、metadata 分别记录 `rotation_history`；JWT 成功/DB 失败时命令
  非 0，但 JWT 可用、旧令牌仍可验签；不再声称"全有或全无"。
- **配置注入**：`FKH_JWT_PREVIOUS_SECRETS` 属于部署管理信任边界；当前只由 `serve` 注入，
  并限制数量/去重。未额外限制来源文件（能改环境的人同样能改当前密钥）。
- **审计**：`rotation_history` 记录最近 20 条事件（jwt_rotated / db_app_rotated /
  db_app_rotated_unconfirmed / db_app_replace_failed / db_app_verify_failed /
  jwt_previous_retired），不含密钥；`retrieval_key_id` 区分代次。跨代次 query hash 不自动关联，
  已在 docs 边界写明。

## 三、测试与证据

- 新增 `tests/unit/test_rotation_helpers.py`：
  - `OperationalError` → `_PasswordChangeUncertain`；
  - `DatabaseError` → 确认拒绝，且未 commit；
  - pending `.new` → `serve/rotate/retire` 拒绝；
  - `os.replace` 失败 → 旧文件不变、`.new` 保留。
- `uv run pytest -q` → **689 passed**；`ruff`/`mypy` 全绿。
- `scripts/acceptance_instance_rotation.py` → **17 步全 ✅**：
  旧令牌/旧 DB 口令 → rotate → 宽限期旧令牌 200、新令牌 200、旧 DB 口令失败、新 DB 口令成功
  → retire → 旧令牌 401、新令牌 200；previous ACL private、metadata 无密钥。
  证据：`_evidence/instance_rotation_acceptance.md`。
- 仍无法自动注入"提交后连接中断"的真实 PG 故障；该路径由异常分类单测 + 显式事务代码固定。

## 四、请 L1 判断

1. `OperationalError/InterfaceError` 先于 `DatabaseError` 捕获、显式事务 commit 作为确认点，
   是否足以解除 MUST_FIX？
2. 不确定时保留 `.new`、不停 PG、pending 文件阻断 serve/rotate/retire，是否满足可运维要求？
3. 其余 SHOULD_FIX（kid、跨代 query hash、轮换历史）是否可留到下一轮？请给 PASS 或最小 MUST_FIX。

## 五、固定回复骨架

```text
VERDICT: PASS | CHANGES_REQUESTED

MUST_FIX:
- <问题>: <为什么阻塞> -> <最小修复>

SHOULD_FIX:
- <问题>: <建议>

BOUNDARY:
- <本轮不要求覆盖的项>
```
