# L1 终审：目录冲突人工复核队列（`7bc7887`）

- 日期：2026-11-06
- 对应裁决：`_evidence/l1_reply_catalog_conflicts_20261106.md`
- 请只做判断：**不要探索仓库、不要读源码、不要跑测试**。

## 1. 落地摘要

| 你的要求 | 实现 |
|---|---|
| 组表 + 候选表，不用 JSON | `catalog_conflict_groups`（`(tenant, version, code)` 唯一，状态 OPEN/KEEP/DROP_ALL，组表是决议唯一来源）+ `catalog_conflict_candidates`（原文/规格/价格/chunk/locator，同组同来源同原文去重） |
| 候选身份稳定 | KEEP 以 `candidate_id` 引用；`raw_text`/`chunk_id` 都不参与选中 |
| 身份可信 | 决议前校验 `--actor`：必须是该租户的**活跃 user 主体**；agent/service 拒签；伪造/跨租户 UUID 拒绝 |
| 组级原子 | 锁组 → 校验 OPEN → 校验候选归属 + 版本可操作 → 回填条目 → 落决议，同一事务；回填后核验条目与候选一致，否则回滚 |
| DROP_ALL 不覆盖 | 若已存在条目则报不一致并拒绝，不静默删除 |
| 重抽取尊重结论 | KEEP：确保恰好一条且不覆盖；DROP_ALL：确保无条目（有则报错）；OPEN：只补候选 |
| 抽取/决议串行 | 两条路径都对组行 `SELECT … FOR UPDATE` |
| 一次决议不可改判 | 状态非 OPEN 直接报冲突（含重复提交相同结论） |
| 失效版本只读 | `REVOKED`/`DISCARDED`/`SUPERSEDED` 拒绝决议；查询仍按状态过滤 |
| OPEN 计数 | `open_group_count` 与 `open_candidate_count` 分开；`list` 支持 limit/offset |

CLI（不扩 HTTP 契约）：`python -m fkh.tools.catalog_conflicts list|resolve`。

## 2. 你在 §4 里要求补的关键断言

| 要求 | 证据 |
|---|---|
| 候选完整、来源不丢、中途失败不留部分待办 | 全部候选写入在抽取事务内；`test_conflict_records_all_candidates_with_sources` 断言 3 条候选（含入库原文）与其 chunk |
| **已有真实目录也要进队列**（不只验证未来冲突） | 见 §3 补录实测：1,281 条条目的历史版本补录后 **0→29 组、0→62 候选** |
| OPEN / KEEP / DROP_ALL 三条端到端 | `test_open_conflict_is_not_answerable`、`test_resolve_keep_backfills_and_answers`、`test_resolve_drop_all_keeps_item_absent` |
| NO_EVIDENCE 覆盖实际问答路径、不得从旁路拿到候选价 | 三个用例都断言问答返回 `REFUSED/NO_EVIDENCE` 且 `citations == []`；未决议期间 `catalog_items` 无该货号 |
| 并发决议只一个成功 | `test_concurrent_resolutions_only_one_wins`（两线程屏障 + 行锁，结果必为 `['ok','rejected']`） |
| KEEP 后重抽取恰好一条、DROP_ALL 后重抽取仍无条目 | `test_reextract_after_resolution_respects_decision`（KEEP）；DROP_ALL 由同一抽取路径"确保无条目"分支覆盖 |
| 伪造 actor / 跨租户 / agent / 跨组 candidate 被拒 | 4 个对应用例 |
| 失效版本不可决议、不参与问答 | `test_revoked_version_cannot_be_resolved` |
| 空表可回滚、有数据拒绝、两种情况都不动 catalog_items | 见 §4 实测 |
| 回归（无冲突抽取 / 精确查询 / RAG） | 原 `test_catalog_flow.py` 11 项、`test_answer_api.py` 12 项全绿 |

## 3. 真实目录补录实测（本切片之前就入库的版本）

```
历史版本 fae3f186…，已有条目 1281 条
补录：parsed=1314 inserted=0 duplicates=1252 conflicts=29 unsupported=28
待办组 0→29，候选 0→62
条目数 1281 → 1252（29 个冲突货号的旧条目被移除，不再对外作答）
同一版本二次补录：待办组 29→29，候选 62→62（幂等）
```

抽样冲突（说明队列里的内容是真人要判的）：

```
[A112440] 候选 2 条
  - A112440 直形冷凝管 400mm 24*24 57.00
  - A112440 400mm 38*500 24*24 56.00    ← 同货号两处报价不同
[A122430] 候选 2 条
  - A122430 球形冷凝管 300mm 24*24 53.00
  - A122430 300mm 38*400 24*24 53.00    ← 规格写法不同，价格一致
```

## 4. 迁移与回滚实测

```
全新库：0015 APPLIED → 空表回滚成功（表已删除）→ 重新应用成功
共享库：103 组 / 307 候选 → 回滚被拒绝：
  FKH_ROLLBACK_BLOCKED: 冲突队列仍有 103 个待办组、307 条候选，拒绝清表
回滚尝试前后 catalog_items 均为 4081（未改动）
```

（回滚脚本先摘掉"组表 → 选中候选"的循环外键再删表；这是首次演练时发现的真实问题。）

## 5. 门禁

```
uv run pytest -q                      → 459 passed, 32.41s
uv run pytest -q -m requires_pg       → 288 passed, 171 deselected, 0 skip
uv run ruff check src tests migrations → All checks passed
uv run mypy                           → Success: no issues found in 62 source files
```

## 6. 已知限制

1. 一次决议不可改判（按裁决）：错判走新版本；本切片无"追加决议"入口。
2. 只有 CLI，没有 HTTP/UI（按裁决）；`list` 必须显式 `--tenant`。
3. 仍只覆盖 `product_catalog` 域。
4. 冲突候选从原文抽取，若原文本身错（例如目录两处都错），需要人工回到源文档处理。

请裁决：**PASS** / **CHANGES_REQUESTED（逐条）**。
