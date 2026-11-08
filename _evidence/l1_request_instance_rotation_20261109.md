# L1 送审：实例密钥轮换（JWT 宽限期 + DB 应用口令）

- 日期：2026-11-09
- 基准：`315cb4d`（本轮修改前）
- 沙箱：`read-only`；**不要探索仓库**，只基于本文事实与片段判断。
- 预算：本切片 L1 调用 1–3 次。

## 一、目标与边界

补齐 `docs/INSTANCE-LIFECYCLE.md` 记录的缺口：「没有密钥轮换流程」。
本轮只做**停服式**轮换，不做零停机双写；管理员 DB 口令不在此命令内。

## 二、实现

### 1) JWT 多密钥验签（`fkh/api/security.py`）

- `Settings` 新增 `jwt_previous_secrets: list[str]`（默认空）。
- `serve` 把当前密钥放 `FKH_JWT_SECRET`，旧密钥放 `FKH_JWT_PREVIOUS_SECRETS`（JSON 数组）。
- `decode_token` 候选项 = `[current, *previous]`；逐个用固定 `HS256`、固定 issuer/audience/required claims 验签，命中即通过；全部失败统一 `UNAUTHENTICATED`，不暴露是哪把密钥失败。
- 旧密钥**只验签、不签发**；新令牌始终用当前密钥签。

### 2) 轮换命令（`fkh/tools/instance.py`）

```text
rotate-keys --workdir <dir> [--force]
retire-jwt-previous --workdir <dir>
```

`rotate-keys` 要求 API 已停止（端口无监听 + pid 文件 verdict 不是 ours），然后：

- JWT：
  1. 当前 `jwt.secret` 写入 `jwt.previous.secret`（覆盖上一轮宽限期密钥）；
  2. 新密钥先写 `jwt.secret.new`，再 `os.replace` 成 `jwt.secret`；
  3. 若 `jwt.previous.secret` 已存在且未加 `--force` → 拒绝，避免悄悄淘汰更早令牌。
- DB 应用口令：
  1. 生成新口令，写入 `db-app.password.new`（私有 ACL）；
  2. 管理员连接执行 `ALTER ROLE "<app_role>" PASSWORD ...`；
  3. `os.replace` 成 `db-app.password`；
  4. 用新口令真实 `SELECT 1`；
  5. 若 `rotate-keys` 启动过内嵌 PG（原先是停止状态），轮换后停回去。
- 元数据：`instance.json` 只更新 `rotation_seq` / `jwt_key_id` / `retrieval_key_id` / `jwt_rotated_at` / `db_app_rotated_at`，不含密钥；写 metadata 也是临时文件 + `os.replace`。
- 审计：`FKH_RETRIEVAL_HASH_KEY` 仍从当前 JWT 密钥派生；`retrieval_key_id` 改为 `k-<name>-r<n>`，旧审计行保留旧 id。

`retire-jwt-previous`：要求 API 停止，删除 `jwt.previous.secret` 并记录 `jwt_previous_retired_at`；下次启动后旧令牌 401。

### 3) 失败/部分成功语义

- JWT 先轮换；若 DB 步骤失败，命令返回非 0，但 JWT 已是新密钥 + 旧密钥宽限，`instance.json` 已记录 JWT 轮换；旧令牌仍可验签，服务可启动。
- DB `ALTER` 失败：删除 `.new`，旧口令/旧文件不变；JWT 已轮换（上一段）。
- `ALTER` 成功但文件 `os.replace` 失败：DB 已用新口令、文件仍是旧口令；命令报错并把新口令留在 `db-app.password.new`，要求人工替换后重启。
- 新口令登录失败：命令报错；文件/DB 已是新口令，需人工排查。

## 三、证据

- 单测：`decode_token` 在宽限期内接受旧密钥、退役后拒绝旧密钥、未知密钥即使有 previous 也拒绝。
- 验收：`scripts/acceptance_instance_rotation.py`（临时实例，端口 55499/8169）：
  - 轮换前旧令牌 200、旧 DB 口令可登录；
  - `rotate-keys` 后：当前 JWT 改变、`jwt.previous.secret` == 旧 JWT、DB 口令改变、`instance.json` 记录 `jwt-2` / `k-...-r1`、previous 文件 ACL private、metadata 无密钥；
  - 重启后：旧令牌 200（宽限期）、新令牌 200、旧 DB 口令失败、新 DB 口令成功；
  - `retire-jwt-previous` 后重启：旧令牌 401、新令牌 200；
  - 17 步全 ✅，证据 `_evidence/instance_rotation_acceptance.md`。
- `uv run pytest -q` → **685 passed**；`ruff` / `mypy` 全绿。

## 四、请 L1 判断

1. **多密钥验签**：无 `kid`、固定 HS256、逐个尝试 current+previous，是否可接受？是否需要在令牌头引入 `kid` 才能避免算法混淆/性能/审计问题？（当前只有 HS256，且候选项来自实例配置。）
2. **宽限期策略**：只保留一把 previous、二次轮换默认拒绝、`--force` 才覆盖，是否足够？`retire-jwt-previous` 要求停服是否合理？
3. **DB 口令轮换**：`.new` → `ALTER ROLE` → `os.replace` → 新口令登录验证 的顺序与失败语义是否可接受？`ALTER` 成功后文件替换失败时把新口令留在 `.new` 并报错，是否满足"可运维、不静默"？
4. **部分成功**：JWT 已轮换、DB 失败时命令返回非 0 但保留可用状态；是否必须做成全有或全无？在 PG DDL 无法回滚口令的情况下，最小合理语义是什么？
5. **配置注入**：`FKH_JWT_PREVIOUS_SECRETS` 由 `serve` 环境注入；如果部署者手工注入任意密钥，会扩大可验签集合。这个信任边界是否需要额外限制（例如限制长度/数量/来源文件）？
6. **审计**：轮换后 `retrieval_key_id` 变化、`query_hash_key_id` 旧行保留，是否满足审计可追溯？是否还缺"轮换事件"审计记录（当前只写 instance.json）？

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

- 只审密钥轮换；混合检索生产启用/客户语料验收不在本轮。
- 不要探索仓库；如需片段请点名。
- 本轮 L1 只判断，不要求跑测试或改代码。
