# L1 复审：目录规则读写锁协议（`776a259`）

- 日期：2026-11-08
- 上一轮回执：`_evidence/l1_reply_catalog_rules_impl_20261108.md`（有条件通过，剩余条件 = 读写锁协议）
- 请只做判断：**不要探索仓库、不要读源码、不要跑测试**。

## 1. 你指出的问题与修复

> 写入事务可能读取旧规则，规则更新检查时看不到该事务尚未提交的条目，随后双方提交，留下不合规数据。

修复（`776a259`）：

| 角色 | 锁 |
|---|---|
| 写入路径读规则 | `SELECT … FROM catalog_rules WHERE tenant_id = %s **FOR SHARE**`，**持有至事务结束** |
| PUT 更新规则 | `SELECT … **FOR UPDATE**` → 存量兼容性检查 → 更新 → 写审计版本，同一事务 |
| 读路径（GET 配置） | 无锁读取 |

写入路径覆盖：入库抽取（`repository.extract_catalog_items`）、冲突决议（`resolve_group`）、
回填 CLI（`catalog_backfill`）。三者共用 `require_rule(..., for_write=True)`，
未显式传规则时仓库层自行加载（不会出现"漏传就不校验"）。

语义：共享锁的写入事务先提交 → PUT 的排他锁等待结束后能**看到**这些行再做兼容性检查；
PUT 先持排他锁 → 写入事务在读取规则处等待，读到的必是新规则。

## 2. 验收（真实 PG，两条/三条连接 + `lock_timeout`）

`tests/e2e/test_catalog_rules.py::test_rule_writes_and_updates_are_mutually_exclusive`：

1. 连接 A（写入事务）取规则 `FOR SHARE` 且未提交 → 线程 B 调 `save_rule` →
   **阻塞至 `lock_timeout=1500ms`，抛 `LockNotAvailable`**（PUT 不能抢跑）；
2. 连接 A 回滚后，连接 B 用 `save_rule` 持有 `FOR UPDATE` 未提交 → 线程 C 调
   `load_rule(for_write=True)` → 同样 `LockNotAvailable`（写入不能绕过新规则）。

两个方向都断言"不是静默成功"，而是真实的锁等待超时。

## 3. 同帧其它确认

- 普通文档（非 `product_catalog`）仍可入库：符合你的判断，本片未改；
- 收紧冲突 409、固定公开文案、不新增"不合规货号列表"契约：按你的意见保留；
- **查询侧识别**（EAN 等）未改造，单独立项——本轮不宣称 EAN 查询识别已支持；
- `_assert_compatible` 仍检查历史条目对新规则的兼容性（只是在 PUT 时检查，不回溯重抽）。

## 4. 门禁

```
uv run pytest -q            → 644 passed   exit=0
uv run ruff check src tests → All checks passed!  exit=0
uv run mypy                 → Success: no issues found  exit=0
```

## 5. 遗留（本片外，已记录在 docs/SPEC/catalog-rules.md §6）

- 查询侧货号识别仍用通用兜底正则（纯数字 EAN 可能与价格/数量混淆）；
- 规则变更不回溯已抽取的历史条目；
- `catalog_rule_revisions` 只记录 PUT，不记录"某次回填用了哪版规则"。
