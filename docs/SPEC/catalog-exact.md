# 目录结构化通道（货号 / 单价精确查询）

- 状态：已实现（2026-11-06，L1 有条件通过 → 落地）
- 裁决记录：`_evidence/l1_reply_catalog_exact_20261106.md`（有条件通过）
- 终审：**PASS**（`_evidence/l1_reply_catalog_final_20261106.md`）——五项补充要求全部闭环
- 起因：客户目录评测（`_evidence/catalog_benchmark_20261106.md`）——30 条货号查询，
  文本 RAG 的 recall@10 词法 **0/30**、混合 **2/30**；端到端 `ANSWERED` **0/10**、引用 **0/10**。
  已排除切块因素（整行完整存在于块内），属"精确匹配任务被模糊检索处理"。

## 1. 定位

| 能力 | 走哪条路 | 是否经过模型 |
|---|---|---|
| 单货号 + 询价/询规格 | **目录精确通道** | **否**（确定性规则） |
| 其余问题 | 既有 RAG | 是 |

精确通道解决三件事：检索排序不适合编号、14B 不遵守引用协议、价格是确定性事实。

## 2. 抽取（入库时）

- 只在 `domain='product_catalog'` **且**显式给出 `catalog_currency` 时执行；
  没给货币不抽（价格口径必须有来源，不默认人民币）。
- 只认「行首货号 + 行尾**单一**价格」的行（`^[A-Z]{1,3}\d{5,8}\s+…\s+\d{1,6}(\.\d{1,2})?$`）。
- 区间价、缺价格、重复货号冲突**一律不猜**：记 `unsupported`/`conflicts` 计数并进 ingestion warnings。
- 与入库同事务；抽取查询原始 child 块，结构化行绑定 `chunk_id` 与 `locator`。
- 幂等：同版本内同货号同原文行 → `duplicates`；同货号不同原文行 → `conflicts`，
  **两条都不保留**（宁可查不到，也不给错价——冲突需要人来判断哪条对）。

## 3. 查询（`catalog_items`）

必须同时满足：

1. 版本状态 ∈ `APPROVED_INTERNAL` / `PUBLISHED_PUBLIC`；
2. `required_scope` 等于本次请求的 `knowledge_scope`；
3. `effective_at`/`expires_at` 生效窗口；
4. 来源块的 ACL 对当前主体 `ALLOW`。

不满足任一条 → **对外表现与"不存在"一致**（不泄露条目存在性）。

**跨版本歧义**：同一货号在不同可检索版本里对应不同（规格, 价格）→ `REFUSED/CONFLICTING_EVIDENCE`，
不任取一条；事实相同才按版本号降序取最新版本作引用来源。

**原文一致性**：引用块的正文里必须能找到 `raw_text`，否则 `HUMAN_REVIEW_REQUIRED`，
不用结构化值硬答。

## 4. 路由（`AnswerService`）

`detect_catalog_intent(query)` 确定性规则：

- 恰好**一个**不同货号（重复出现算一个）；≥2 个或 0 个 → 不接管；
- 且问题含价格词（单价/价格/报价/多少钱/售价/价位）或规格词（规格/尺寸/参数/型号/标口/管径/口径）；
- 「这个货号是否停产」这类**看到货号但不在支持意图内**的问题 → 回 RAG，审计记 `code_without_intent`。

未命中/歧义/不一致都**不调用模型**（`generator` 调用次数为 0）。

## 5. 审计

`rag_queries` 新增两列（0014）：

- `route`：`rag` | `catalog_exact`；
- `route_reason`：`no_code` / `multi_code` / `code_without_intent` / `accepted` / `not_found` /
  `ambiguous` / `inconsistent`（短枚举，**不写原始查询**）。

精确通道 `model_id = 'catalog-exact:v1'`：它代表**确定性执行路径**，
统计模型调用量与生成耗时时不得计入真实模型。

一次精确请求恰好一条 `rag_queries`；成功单条命中恰好一条 `rag_citations`；
未命中/歧义/不一致为 0 条引用。审计写不进去 → 抛错，不返回成功。

## 6. 补录（历史文档）

`uv run python -m fkh.tools.catalog_backfill --url <dsn> --currency CNY [--tenant <uuid>] [--dry-run]`

可重复执行：已存在的行只计 `duplicates`，不改原文、不动版本状态与 ACL。

## 7. 迁移与回滚

- `0014_catalog_items.sql`：建表 + RLS(FORCE) + `rag_queries` 两列；
- `rollback/0014_down.sql`：**有数据则拒绝**（不自动清表），提示这些事实可由原文重抽。

## 8. 验收证据

`tests/e2e/test_catalog_flow.py`（10 项）：抽取与 unsupported 计数、非目录域/缺货币不抽取、
精确命中（无模型）、未命中（无模型）、多货号/非意图不接管、版本撤销后不可见、跨版本歧义拒答、
结构化与原文不一致转人工、跨租户不可见、补录幂等。

`tests/unit/test_catalog_parse.py`（6 项）：单价格行、区间价、缺价、表头忽略、重复货号、空文本。

## 8.1 真实语料实测（2026-11-06，三爱思 85 页目录）

```
catalog parsed=1314 inserted=1252 duplicates=1 conflicts=29 unsupported=28
用例 20: ANSWERED=20 价格正确=20 恰好1条引用=20
审计路由分布: [('catalog_exact', 'accepted', 20)]
```

关键点：这次 `generator=None`——**没有任何模型参与**，20 条货号/报价问题全部答对并各带 1 条原文引用。
29 条冲突货号被剔除（同一货号在本版本内出现不同原文行），不提供精确答案。

## 9. 已知限制

1. 只支持「单一价格」的目录行；报价区间、阶梯价不支持（会进 `unsupported`）。
2. 只对 `domain='product_catalog'` 生效；其他域的编号类事实未覆盖。
3. 不做同义词/别名匹配（货号必须字面一致，大小写除外）。
4. 不做跨语言/跨单位换算（货币当前只允许 CNY）。
