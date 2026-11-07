# L1 复核：连接工厂放行条件 4（提交 `71d1d5d`）

- 日期：2026-11-07
- 只复核你在 `_evidence/l1_reply_permission_close_20261107.md` 第 4 项提出的放行条件。
- 请只做判断：**不要探索仓库、不要读源码、不要跑测试**。

## 落实方式

唯一入口 `fkh.api.db.tenant_connection(pool, tenant_id)`（上下文管理器）：

```python
with pool.connection() as conn:          # 池子仍由 psycopg_pool 管
    bind_tenant(conn, tenant_id)          # 1) 业务 SQL 之前绑定（会话级 set_config ... false）
    completed = False
    try:
        yield conn
        completed = True
    finally:
        _release(conn, completed=completed)

def _release(conn, *, completed):
    try:
        status = conn.info.transaction_status
        if status == INERROR:      conn.rollback()   # abort 事务先回滚，否则清理语句被拒
        elif status != IDLE:
            conn.commit() if completed else conn.rollback()   # 2) 异常/取消 → 回滚
        conn.execute("SELECT set_config('app.tenant_id', '', false)")
        conn.commit()               # 3) 清理单独提交，不被后续 rollback 撤销
    except Exception:
        conn.close()                # 4) 清理失败丢弃连接
```

业务侧：`get_conn` 依赖与 `/v1/knowledge/answer` 的池用法都改走该入口。

## 把约束变成可失败的检查

1. `tests/gate/test_connection_factory.py`：扫描 `src/fkh/**/*.py`，
   除 `api/db.py` 外出现 `pool.connection(` 即失败（把"记得绑定"变成 CI 约束）。
2. `tests/e2e/test_tenant_connection.py`（真实连接池 + 真实 PG，5 条）：
   - 绑定先于业务 SQL；
   - 异常路径清理；
   - **abort 事务后清理仍生效**（模拟 fail 的 `SELECT * FROM 不存在的表`）；
   - 清理失败丢弃连接（业务代码里 `conn.close()` 使清理语句失败，随后取到的必须是另一条连接）；
   - 两租户交替使用后连接无残留上下文。

## 需要你判断的三点

1. 上述 `_release` 的三路分支（INERROR / 待提交 / 已 IDLE）是否覆盖了你要求的"正常、异常、取消"？
   取消（客户端断开）在我这里表现为端点函数抛异常，走 `completed=False` 分支。
2. "清理失败即丢弃"实现为 `conn.close()`；池在归还时看到已关闭连接会丢弃并补建。
   是否要求额外显式 `pool` 侧剔除（例如记录告警/指标）？
3. 门禁只禁止 `pool.connection(` 字面量；若有人写 `getattr(pool, "connection")()` 之类绕过，
   靠扫描抓不到。是否需要更硬的机制（例如把池包成只暴露 `tenant_connection` 的封装对象）？

## 门禁

`uv run pytest -q` → **593 passed**；`ruff` / `mypy` 全绿。
