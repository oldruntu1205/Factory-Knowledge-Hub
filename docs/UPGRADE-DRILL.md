# 存量升级演练（0021 → 最新）

> 2026-11-08，基线 `1b1701d` 之后。自动化验收：`tests/e2e/test_upgrade_rehearsal.py`（4 条，真实 PG）。

## 1. 演练方式

1. 在集群里建**独立 scratch 库**（不碰开发库与演示库）；
2. 只应用到 `0021`，得到旧版 schema；
3. 用 SQL 造旧结构数据：三个索引代（1/2/3）的块、一个停在 `DRAFT` 的待发布版本、
   同一货号 `A112420` 在两个公开版本里给出不同价格（39.00 / 42.00，升级后应判歧义）；
4. 升级到最新版（`0022`–`0028`）；
5. 业务验收用**非 owner、无 BYPASSRLS** 的业务角色跑 HTTP。

## 2. 验收结果

| 项 | 断言 |
|---|---|
| 回填 | `index_generations` 全部 READY，`index_visibility` 水位 = 最大代 |
| 旧待发布版本 | review PASS 后 publish 200，状态 APPROVED_INTERNAL（未被新闸门卡死） |
| 撤销后立即不可读 | 撤销 200 后检索结果不含该版本；证据直读 403/404 |
| 清理可重试并收敛 | 注入一次失败 → failed>=1；重跑 → processed>=1，该版本块计数 0 |
| 审计追溯 | rag_queries、evidence_access_log、outbox_deliveries(DONE) 均保留 |

## 3. 覆盖不到的

- 生产规模数据量（演练是小样本）；
- `0002` 向量迁移在无 pgvector 环境会被跳过（演练库有 pgvector，未覆盖跳过路径）；
- 回滚路径（`*_down.sql`）本次未演练；
- 升级期间的并发写入未测（逐文件单事务执行）。

## 4. KEEP 决议 + 撤销 + 清理（2026-11-08 补充回归）

`tests/e2e/test_catalog_keep_revoke.py`（真实 PG + 业务角色 + HTTP）。复现与修复：

| 阶段 | 现象 | 修法 |
|---|---|---|
| 首次清理 | `update or delete on catalog_conflict_candidates violates foreign key constraint "fk_group_selected_candidate"`（0015 是 RESTRICT） | `0032` 把该约束改为列级 `ON DELETE SET NULL (selected_candidate_id)` |
| 再次清理 | `catalog_conflict_groups_check` 要求 KEEP 必须有 `selected_candidate_id`，置空后撞 CHECK | `0033` 放宽为"有候选 **或** 有决议快照" |
| 审计 | 候选被级联删除后无法回答"保留的是哪一行" | `0031` 决议时写 `resolved_snapshot`（规格/原文/价格/候选 ID/操作者/备注） |

验收断言：撤销后证据直读立即 403；清理后块/目录条目/候选计数均为 0；冲突组仍是 `KEEP`、
`selected_candidate_id` 为空、`resolved_snapshot` 完整（含 `spec` 与 `raw_price`）、
`resolved_by`/`note` 保留。
