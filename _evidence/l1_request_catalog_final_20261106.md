# L1 终审：目录精确通道（`b74917c`）

- 日期：2026-11-06
- 对应裁决：`_evidence/l1_reply_catalog_exact_20261106.md`（有条件通过）
- 请只做判断：**不要探索仓库、不要读源码、不要跑测试**。

## 1. 落地摘要

| 层 | 内容 |
|---|---|
| 迁移 0014 | `catalog_items`（货号/规格/原文行/原始价格串/`numeric(12,2)`/货币/`chunk_id`/`locator`）；复合外键；版本内货号唯一；**FORCE RLS + tenant_isolation**；`rag_queries` 新增 `route`、`route_reason` |
| 抽取 | 仅 `domain='product_catalog'` **且**显式 `catalog_currency` 才执行（不默认人民币）；只认「行首货号 + 行尾单一价格」；与入库同事务；失败计数进 warnings |
| 查询 | 版本状态 + `required_scope` + 生效窗口 + 来源块 ACL 四者同时满足；不可见统一表现为未命中 |
| 路由 | 恰好一个不同货号 **且** 明询问价/询规格才接管；多货号/无意图回 RAG 并记原因；全程无模型 |
| 审计 | `route`/`route_reason` 短枚举（不写原文）；`model_id='catalog-exact:v1'` 表示确定性路径 |
| 补录 | `fkh.tools.catalog_backfill` 可重复执行，历史目录可补 |
| 回滚 | 有数据即拒绝清表，不动任何行 |

## 2. 逐条对应你的约束

| 你的要求 | 证据 |
|---|---|
| 只对 `product_catalog` 抽取；失败计数只针对候选目录行 | `test_non_catalog_domain_or_missing_currency_extracts_nothing`；表头与说明行不进 unsupported（`test_header_and_prose_are_ignored`） |
| 未命中直接拒答、不回退 RAG、不调用模型、不可区分 | `test_exact_miss_refused_without_model`；跨租户 `test_cross_tenant_lookup_is_miss` 同样表现为 NO_EVIDENCE |
| 多货号回 RAG 并记原因 | `test_multi_code_and_out_of_intent_do_not_take_over`：审计出现 `(rag, multi_code)` 与 `(rag, code_without_intent)` |
| 精确通道不允许模型参与 | 精确路径在 `_catalog_answer` 内完成，不触碰 generator；实测 `generator=None` 仍 20/20 答对 |
| 版本状态查询时校验、不级联删除 | `test_revoked_version_hides_items`；条目行仍存在，仅查询过滤 |
| `numeric(12,2)` + `raw_text` + `raw_price`；CNY 必须显式 | 表结构与 `catalog_currency` 必填（缺则跳过抽取并告警） |
| 不新增 scope、不绕过来源权限 | 入口仍 `knowledge.answer`；查询内做 ACL `evaluate`，不要求 `knowledge.search` |
| 不加 HTTP 端点 | 未加 |

## 3. 你在「必须补入实施口径」里点名的五条

1. **一个货号不足以触发**：`detect_catalog_intent` 还要求价格词或规格词；「这个货号是否停产」→ 回 RAG（审计 `code_without_intent`，已测）。
2. **跨版本歧义**：同一货号在不同可检索版本对应不同（规格, 价格）→ `REFUSED/CONFLICTING_EVIDENCE`，0 条引用（`test_ambiguous_versions_refused_not_guessed`）。
3. **正则 ≠ 价格语义**：区间价/缺价不产条目（单测）；**同版本同货号不同原文行 → 两条都不保留**（`test_same_version_conflict_is_not_served`），既不是覆盖也不是任取；切块重叠按同原文行去重（`duplicates`）。
4. **引用可定位到原文行**：引用取 `chunk_id`/`version_id`/`locator`；查询时校验 `raw_text` 确实出现在块正文里，不一致 → `HUMAN_REVIEW_REQUIRED`、0 引用（`test_structured_data_inconsistent_with_source_requires_review`）。
5. **历史目录补录**：`catalog_backfill` 幂等；`test_backfill_is_idempotent` 断言重复执行 `inserted=0, duplicates=2, conflicts=0`。

## 4. 真实语料实测（三爱思 85 页目录，`generator=None`）

```
catalog parsed=1314 inserted=1252 duplicates=1 conflicts=29 unsupported=28
用例 20: ANSWERED=20 价格正确=20 恰好1条引用=20
审计路由分布: [('catalog_exact', 'accepted', 20)]
```

- 想要的 1,281 条全被抽出；29 条因同版本冲突被剔除（不提供错误价格）、28 条为不支持格式（区间价等）；
- **没有模型参与**仍然 20/20 答对，每条恰好 1 条引用；
- 对比改造前：同一批问题文本 RAG `ANSWERED` 0/10、引用 0/10。

## 5. 迁移回滚实测

```
0014: APPLIED
catalog_items 行数: 90
重复 apply 退出码: 0            ← 版本账本保证重复执行安全
<- 0014_down.sql
rollback 被拒绝: FKH_ROLLBACK_BLOCKED: catalog_items 仍有 90 条记录，拒绝自动清表
回滚尝试后 0014: APPLIED；catalog_items 仍在: 90
```

## 6. 门禁

```
uv run pytest -q                      → 448 passed, 31.25s
uv run pytest -q -m requires_pg       → 277 passed, 171 deselected, 0 skip
uv run ruff check src tests migrations → All checks passed
uv run mypy                           → Success: no issues found in 60 source files
```

## 7. 已知限制（照实写）

1. 只支持单一价格；阶梯价/区间价进 `unsupported`。
2. 别名、跨语言、单位换算未做（货号需字面一致，大小写除外）。
3. 冲突货号当前"不提供精确答案"，尚未有专门的人工复核队列（后续可加状态列）。
4. 结构化事实只覆盖 `product_catalog` 域。

请裁决：**PASS** / **CHANGES_REQUESTED（逐条）**。
