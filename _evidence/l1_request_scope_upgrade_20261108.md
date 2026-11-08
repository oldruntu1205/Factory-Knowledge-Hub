# L1 终审：权限修复与存量升级验收（`cd3a21b`，基线 `1b1701d`）

- 日期：2026-11-08
- 风险归类：**权限/可见性**、**并发与锁**、**DB schema 升级**
- 请只做判断：**不要探索仓库、不要读源码、不要跑测试**。

## 1. 修复一：培训三通道共用课程来源与分级

**复现（修复前，基线 `1b1701d`）**：学员在课程 A（检验规范，来源只有 QC 文档）的任务里问
`A112420 的规格是什么？` → `ANSWERED`，答案
「A112420 的规格为 200mm 38*300 24*24；单价 39.00（CNY）。」，引用指向**课程 B** 的目录版本。
C3 课程源同理：正文读取 409，问答仍能答。

**原因**：目录精确通道在检索**之前**运行，不接受 `version_ids`，也不看 `classification`；
培训问答只给检索通道加了课程来源。

**修复**：`AnswerService.answer(only_version_ids, max_classification)` 把两个约束同时传给
检索通道与**目录通道**（`find_catalog_item` 新增 `version_ids` / `max_classification` SQL 条件）。
培训侧 `approved_source_versions()` 过滤 `classification <= 2`；课程源含 C3+ → 整课不可用，问答 409
（与正文读取一致）。

**验收**（`tests/e2e/test_training_scope_unified.py`，3 条，业务角色 + HTTP）：
课外货号 → 非 ANSWERED、无规格/价格、引用不含 B 版本；本课程 → ANSWERED 且引用恰为课程 A 来源；
审计 route 证明目录通道确实被执行（非被其他分支遮住）；C3 课程正文与问答均 409。

## 2. 修复二：访谈授权撤回

| 问题（修复前） | 修复 |
|---|---|
| 任何持 `interview.create` 的同租户主体都能撤回**他人**授权 | 撤回先 `FOR UPDATE` 锁授权行；`subject_id != actor_subject` → 403（与不存在同码） |
| 重复撤回静默成功 | 第二次 → **409** |
| 撤回与转写/审核/晋升无锁，可能撤回后写入 | 写入路径对授权行取 **FOR SHARE**；撤回未提交时写入阻塞，提交后读到已撤回 → 409 且不落库 |
| 顺带发现：无访谈时撤回 500 | `revoked_versions` 未初始化，已修 |

**验收**（`tests/e2e/test_interview_revoke.py`，5 条，含两条真实连接的并发用例）。

## 3. 修复三：存量升级演练（0021 → 最新）

- 独立 scratch 库只应用到 `0021`；造三代索引、待发布 `DRAFT` 版本、同货号两版不同价格；
- 升级到最新（`0022`–`0028`）后验证：回填 READY + 水位、旧待发布版本可发布、
  撤销即不可读、清理可重试并收敛、审计（`rag_queries`/`evidence_access_log`/`outbox_deliveries`）保留；
- 迁移与造数用管理员，业务验收用**非 owner、无 BYPASSRLS** 角色。
- 文档：`docs/UPGRADE-DRILL.md`。

## 4. 需要你判断的四点

1. **目录通道分级**：我对培训场景传 `max_classification=2`（C3+ 不答）。普通内部问答仍按原口径
   （不限分级，C3 内部知识可答）。这是否符合你对"分级规则"的预期，还是要求内部问答也一律 C0–C2？
2. **C3 课程整课拒绝**：课程源含任一 C3+ 即整课不可用（不部分服务）。我选整课是为了不返回残缺课程，
   若希望"按源逐版本过滤、其余照常回答"，请指示。
3. **撤回的锁粒度**：写入方对**授权行**取 FOR SHARE。这保证与撤回串行，但也意味着同一授权下的
   并发写入彼此不冲突（共享锁），只与撤回冲突。是否接受？
4. **升级演练的覆盖面**：未覆盖生产规模数据量、`0002` 在无 pgvector 环境被跳过的路径、
   回滚脚本演练、升级期间并发写入。是否要求本片补回滚演练（`0022`–`0028` 逐条 down）？

## 5. 门禁与结果

```
uv run pytest -q            → 623 passed   exit=0
uv run ruff check src tests → All checks passed!  exit=0
uv run mypy                 → Success: no issues found  exit=0
```

修复前后对比（`test_training_scope_unified.py`）：修复前 2 failed / 1 passed（含 ANSWERED + 39.00 泄漏）；
修复后 3 passed。访谈撤回：修复前无本人校验、重复撤回 200；修复后 403 / 409 / 并发阻塞。
