# L1 终审：词法查询 AND→OR（`lexical-v2`，`18bcec2`）

- 日期：2026-11-04
- 对应裁决：`_evidence/l1_reply_lexical_and_fix_20261104.md`（裁决 A + §3 三项 + 必须补的断言）
- 请只做判断：**不要探索仓库、不要读源码、不要跑测试**。

## 1. 实现了什么

| 项 | 实现 |
|---|---|
| 查询构造 | `domain/lexeme.build_query`：`segment_cjk` 字级 token 用 ` or ` 连接，每个词项加双引号（裸 `-word` / `or` / 未闭合引号不改变语义）；丢弃无字母数字的纯标点 token；无有效词项返回空串 |
| SQL | `websearch_to_tsquery('simple', build_query(q))`（任意输入不抛语法错误，不需要调用方转义） |
| 空结果 | `build_query` 为空 → 直接返回空集合，不查库、不退化为全量匹配 |
| `scoring` | `lexical.SCORING = "lexical-v2"`；`SearchService` 的映射改为引用该常量，避免两处漂移 |
| 契约 | `search_response.json` enum 改为 `lexical-v2 | hybrid-rrf-v1`（注明 v1 为历史取值、不再返回）；openapi 描述写明 AND→OR 与 recall 变化 |
| 审计 | 迁移 **0012** 扩展 `retrieval_mode` 枚举（0011 已应用不可改）；回滚脚本把 v2 折算回 v1（临时关闭追加式触发器，语句结束恢复） |

## 2. 「必须补的断言」逐条落实

| 要求 | 证据 |
|---|---|
| 查询构造：OR 组合、安全处理操作符、空/纯标点返回空 | `tests/retrieval/test_query_builder.py` 8 项：`冷凝管`→`"冷" or "凝" or "管"`；`冷凝 & \| ! ( ) :*`→`"冷" or "凝"`；`''`/空白/`，。！？；：`→`''`；单字；`ACL 下推`→`"ACL" or "下" or "推"`；重复词项；未闭合引号；`QUERY_FUNCTION == "websearch_to_tsquery"` |
| 边界：单字/短查询/常见字密集/重复词项/中英混合 | 同上；另有端点级用例 `test_query_with_tsquery_operators_is_safe`、`test_punctuation_only_query_returns_empty` |
| 权限与 scope 不变 | 既有 ACL/租户/scope 用例全绿（含 `test_invisible_chunks_never_appear`、跨租户、`test_hybrid_applies_acl_after_fusion`）；本次未改 SQL 的可见性谓词 |
| 结果契约：≤ max_results、允许不足、`scoring=lexical-v2` | `test_denied_candidates_may_underfill_but_never_exceed_limit`、`test_default_config_is_lexical_and_schema_valid`（含 JSON Schema 校验）、审计模式断言 |
| 召回回归 ≥33/50、原 3 条无退化 | 见 §3：34/50；L07/W04/P01 仍命中 |
| 精度证据：P@K、实际返回精度、返回条数、无关条数、最差案例 | 见 §3（**文档级相关性代理**口径，非人工逐条标注） |
| 同环境复测耗时 | 词法 1.8ms → **16.9ms**；混合 82.8ms（CPU，含两路 + 融合 + 取文） |

## 3. 实测（同一语料/评测集/subject/scope，K=10，CPU）

语料：仓库 19 篇 Markdown、约 190 个 child 切片；评测集 50 题（`_evidence/recall_cases_20261104.json`）。

|  | 词法 AND（旧） | **词法 OR（新）** | 混合 |
|---|---|---|---|
| recall@10（片段） | 0.060 (3/50) | **0.680 (34/50)** | 0.840 (42/50) |
| doc@10 | 0.060 | **0.800 (40/50)** | 0.960 (48/50) |
| P@10（文档级代理） | — | **0.162** | 0.232 |
| 实际返回精度（同口径） | — | 0.162 | 0.232 |
| 平均耗时 | 1.8 ms | 16.9 ms | 82.8 ms |

- 回归门槛（≥33/50）：**34/50，通过**；原先命中的 3 条（L07/W04/P01）仍命中，无退化。
- **精度口径是代理指标**：只判断「命中块是否来自含答案的那篇文档」，因此**高估**真实精度、
  且不是人工逐条相关性标注。逐题 top-10 全文与返回条数在 `_evidence/recall_report_20261104.json`。
- 未命中的 8 条（H05/H07/H08/H09/H10/E01/N01/N03）两路都未命中，原因是答案串落在相邻切片或
  同一事实在别的文档里换了措辞；doc@10 把这些算作找回（0.80 / 0.96）。
- 精度下限：你说过「必须在查看结果前确定」，因此**本轮不设下限、也不宣称达标**。请给出下限，
  或判定「先按代理口径接受、客户语料阶段再定人工标注口径」。

## 4. 门禁（独家运行）

```
uv run pytest -q                      → 379 passed, 20.83s
uv run pytest -q -m requires_pg       → 230 passed, 149 deselected, 0 skip
uv run ruff check src tests migrations → All checks passed
uv run mypy                           → Success: no issues found in 51 source files
```

请裁决：**PASS** / **CHANGES_REQUESTED（逐条）**；并请给出精度下限或明确延后到客户语料阶段的处置。
