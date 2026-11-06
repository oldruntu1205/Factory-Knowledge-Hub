# L1 终审：词法排序 v3（BM25 字级重排，`1e58de2`）

- 日期：2026-11-06
- 对应裁决：`_evidence/l1_reply_lexical_bm25_20261106.md`
- 请只做判断：**不要探索仓库、不要读源码、不要跑测试**。

## 1. 你点名的两处缺口

**(a) `avg_len` 等的统计总体**：`N / df / avg_len` 现在都取自**同一可见性范围的全部块**
（租户 + `required_scope` + 版本状态/受众 + `lexeme_tokenizer_id`），且**按 ACL 分组**在 Python 侧求值——
不可见块不参与统计。有专门用例：塞入 30 个 DRAFT「试管」块后，可见结果的 top-3 **逐条相同**。

**(b) "其余查询不受影响"的说法撤回**：改成"已入档的已知退让"——

- 冷凝管、塞子、培养皿：目标块仍第 1；
- 标口范围：目标块由第 2 落到第 3（BM25 更看重判别词，牺牲少量关键词密度型排序）；
- 冻结用例整体 0.48 → 0.62，**改善 8 条、退化 1 条（H05）**，未修改任何预期答案。

## 2. §4 六项裁决的落实

| 裁决 | 落实 |
|---|---|
| 窗口 200、显式拒绝冲突 | `window = min(OVERFETCH×max_results, 200)`；`max_results > 200` 抛 `ValueError`；`SearchOutcome.window/truncated` 暴露截断 |
| df 一次往返 | 单条 SQL（UNION ALL：ACL 级汇总 + 逐词 df）；per-term 用 `plainto_tsquery` 兼容运算符输入；**未新增统计表** |
| k1/b 固定 | 1.2 / 0.75 常量化，无配置 |
| 迁移 0016 | 追加 `lexical-v3`，保留 `lexical-v2`；重复 apply 幂等；有 70 行 v3 审计时回滚被拒 |
| 无长期双模式配置 | **未接入任何 Settings**；`LexicalRetriever.search(scoring=...)` 仅测试/基准用，生产路径恒为 `SCORING`（可随时删除） |
| 冻结基准 | 50 条用例 v2/v3 对比已入档（逐条改善/退化清单在 `_evidence/lexical_v3_benchmark_20261106.md`） |

## 3. 实测数字

**检索基准（冻结 50 条）**

| 版本 | recall@10 | 中位耗时 |
|---|---|---|
| v2 `ts_rank_cd` | 0.48 | 12.4 ms |
| **v3 BM25** | **0.62** | 27.6 ms |

**客户目录（111 块）**

- 「有哪些规格的试管？」→ 试管块进 **top-3**（e2e 断言）；此前 top-5 无试管块；
- 冷凝管/塞子/培养皿仍第 1；标口范围 2→3（已入档）。

**性能（5000 块合成）**

- 中位 **141.9 ms**，P95 209.9 ms；耗时主要在全范围统计。
- 相对模型耗时（秒级）可接受；**10 万块级会到秒级**，需要统计缓存/统计表——本切片不做，列为后续项。

**答案回归（17 条）**：14/17 → **15/17**（`spec-02` 修好）。
剩两条都是**生成侧**，不是排序：

- `tax-04`：语料 0 处税费信息，模型仍答「不含税」；
- `tube-05`：实测提示词里已有 6 个证据块且含「试管」，模型仍说"并未提及试管的规格"（14B 读不了表格）。

## 4. 契约与测试的强制更新（未放松语义）

1. `contracts/schema/search_response.json` + OpenAPI：`scoring` 枚举追加 `lexical-v3`（保留 v2）；
2. `tests/e2e/test_search_hybrid_api.py`：断言由 `lexical-v2` 改为 `lexical-v3`，
   仍要求"实际执行的模式如实回报"；
3. `tests/e2e/test_lexical_ranking.py`（新增 5 项）：判别词 top-3、样板词仍第 1、
   空/标点查询、`max_results` 超窗被拒、**不可见块不影响统计与排序**。

## 5. 门禁

```
uv run pytest -q                      → 482 passed
uv run pytest -q -m requires_pg       → 302 passed, 180 deselected, 0 skip
uv run ruff check src tests migrations → All checks passed
uv run mypy                           → Success: no issues found in 63 source files
```

## 6. 已知限制

1. 窗口截断：目标块若在候选窗口之外仍会漏（当前窗口 200），诊断字段只暴露截断不兜底；
2. 大租户统计成本线性增长（10 万块级需缓存/统计表）；
3. `scoring` 参数虽未接配置，但仍是内部可调用入口；
4. 混合模式（RRF）已跑通，但 BM25 与向量融合后的效果只做了回归，没有单独基准对比。

请裁决：**PASS** / **CHANGES_REQUESTED（逐条）**。
