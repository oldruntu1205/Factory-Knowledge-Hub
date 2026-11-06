# L1 送审稿：目录冲突的人工复核队列（运营待办 + 决议回填）

- 日期：2026-11-06
- 提出方：L2 实施
- 风险归类命中：**DB schema 与迁移**、**人工复核工作流**、**审计**
- 请只做判断：**不要探索仓库、不要读源码、不要跑测试**。

## 1. 现状（上一切片的遗留）

`catalog_items` 已上线，抽取时**同版本同货号不同原文行**的处理是"两条都不保留"
（宁可查不到，不给错价）。这是安全的方向，但**运营看不见发生了什么**：

- 真实目录实测：`parsed=1314 inserted=1252 duplicates=1 conflicts=29 unsupported=28`
- 29 个冲突货号被静默剔除，既没有待办、也没有候选原文可供人判断；
- 结果：这些货号被永久"查不到"，且没人知道该处理它们。

本次要补的是**复核队列**：把冲突候选完整记录 → 运营可列出待办 → 人工决议 → 决议结果回填到
`catalog_items`（或明确"该货号不设条目"），并且决议后再次抽取**不得重新入队**。

## 2. 拟议实现

1. **迁移 `0015_catalog_conflicts.sql`**：新表 `catalog_item_conflicts`
   `(conflict_id, tenant_id, version_id, code, chunk_id, raw_text, spec, raw_price,
     status ∈ {OPEN, KEEP, DROP}, resolved_by, resolved_at, note, created_at)`
   - 每个**候选原文行**一行；同一 `(tenant, version, code)` 的多行构成一个待办组；
   - `UNIQUE (tenant_id, version_id, code, raw_text)`（重复抽到同一行不重复排队）；
   - FORCE RLS + `tenant_isolation`，与既有表一致。
2. **抽取侧**：发现冲突时，把该 code 的**全部候选**写入 `catalog_item_conflicts(status='OPEN')`
   （同一原文行去重），`catalog_items` 不写该 code；已存在 OPEN/决议记录则跳过（幂等）。
3. **运营入口（CLI，不扩 HTTP 契约）**：`uv run python -m fkh.tools.catalog_conflicts`
   - `list [--tenant] [--status OPEN]`：按 (tenant, version, code) 分组打印候选行（含 chunk、原文、计数）；
   - `resolve --tenant --version --code --keep <raw_text 或 chunk_id> --actor <uuid> [--note]`：
     该候选置 `KEEP`，其余置 `DROP`，**并把 KEEP 行写入 `catalog_items`**；
   - `resolve ... --drop-all --actor`：全部 `DROP`，该货号不设条目；
   - 结论：CLI 走数据库连接与显式 `--actor`，不新增 API scope（与"本切片不加 HTTP 端点"一致）。
4. **回填**：决议 KEEP 时写 `catalog_items`（沿用同事务、唯一约束）；DROP_ALL 不写。
5. **幂等**：重复抽取遇到已存在记录（任何状态）不再入队、不重复写条目；重复决议报冲突。
6. **可追溯**：`resolved_by`/`resolved_at`/`note` 落库；不做"撤销决议"（要改只能新增决议记录？见待判 Q4）。

## 3. 待判问题

1. **记录粒度**：每个候选行一行（我的方案），还是每个待办组一行 + JSON 候选数组？
2. **谁有权决议**：CLI + `--actor`（数据库权限把门，不新增 scope）是否足够？还是必须走 API +
   `knowledge.review` 之类权限？
3. **决议后是否回填**：KEEP 时直接写 `catalog_items`（我的方案），还是只标记、等下一次抽取决胜？
4. **决议能否更改**：我倾向"不可改"（一次决议生效，错判要修正只能靠新版本或后续迁移补正），
   避免审计上出现两次相反记录；若你认为应允许改判，请给出口径（保留历史的方式）。
5. **未决议冲突是否要计数暴露**：CLI 的 `list` 是否必须给出"每个租户的 OPEN 数量"（运营看板用）？
6. **版本撤销/弃用后**：冲突记录是否保留（我倾向保留、只读；不随版本删除）。
7. **回滚 0015**：与 0014 一致——有数据则拒绝清表？

## 4. 实施侧计划要补的断言

1. 冲突不静默丢：每个候选行的 chunk、原文、规格、价格都可追溯；
2. 决议前：该货号在问答里**查不到任何候选价格**（仍是 NO_EVIDENCE）；
3. 决议 KEEP 后：`catalog_items` 恰好 1 行、与该候选一致；问答返回该价格 + 正确引用；
4. 决议 DROP_ALL 后：问答仍 NO_EVIDENCE，且**不产生** `catalog_items` 行；
5. 幂等：重复抽取不重新入队、不重复写条目；重复决议被拒绝；
6. 租户隔离：跨租户 list/resolve 看不到也改不了（RLS + 显式 tenant 条件）；
7. 回滚：有冲突数据时拒绝清表，且不改动 `catalog_items`；
8. 现有行为不变：无冲突的抽取、精确通道、RAG 路径回归不受影响。

## 5. 边界与预算

- 不探索仓库、不写代码、不跑测试；一次调用回完 §3 的 1–7 + 对 §4 的裁剪。
- 本切片 L1 调用计划：本次（裁决）+ 实现后终审，共 2 次。
