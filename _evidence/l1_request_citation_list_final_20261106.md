# L1 终审：引用清单协议落地（`893a1ed`）

- 日期：2026-11-06
- 对应裁决：`_evidence/l1_reply_citation_list_20261106.md`
- 请只做判断：**不要探索仓库、不要读源码、不要跑测试**。

## 1. 落地摘要（逐条对应你的口径）

| 你的要求 | 实现 |
|---|---|
| 引用行剥离，但不得全局删「引用：」/UUID | 只删**协议规定的末尾引用行**（逐行匹配 `^\s*引用[:：]`）；正文里的内联 `[id]` 保留，展示时转成 `[1]/[2]` |
| 不裸露内部 id | `_render_citations`：有效 id → `[序号]`，白名单外 → `[?]` |
| 逐句覆盖退出状态判定，不设替代闸门 | 已删除降级逻辑；`uncovered_sentences` 仅作诊断，docstring 明确"是标注完整度，不是证据覆盖率" |
| `引用：无` → 零引用 + 人工 | 走 `INSUFFICIENT_EVIDENCE` + `requires_human_review=true` |
| 「无」不覆盖合法内联；记协议冲突 | `extract_citations` 返回 `explicit_none`；若同时有内联 id，以合并结果为准并 `logger.warning` 记录协议冲突 |
| 伪造 id 维持人工，任一来源都拦 | `build_citations` 对清单与内联合并后的集合做白名单校验；未知 id → `HUMAN_REVIEW_REQUIRED`，可用引用保留 |
| 审计不扩表，但要区分原因 | `_refusal_reason` 产出 `no_citation_line / explicit_none / empty_answer / invalid_ids / no_valid_citation`，只进日志（带 trace_id，不含 query） |

展示正文还额外剥离了「答案正文：」这类模型噪声标签。

## 2. 协议格式（提示词末尾，模型对末尾指令更敏感）

```
【回答格式（必须遵守）】
先写答案正文：用完整句子，先说结论再给依据，不要只写一个词或短语；
正文结束后**必须另起一行**，原样写：
引用：<evidence_id> <evidence_id> …
如果无法回答，最后一行写：引用：无
```

## 3. 实测（qwen3:14b，同一批真实目录问题）

| 指标 | 旧协议（逐句内联） | 新协议（清单） |
|---|---|---|
| ANSWERED | 0/6 | **5/6** |
| 带引用 | 0/6 | **5/6** |
| 内容关键词命中 | —（8 题样本 6/8） | 4/6 |

**必须说清楚的代价**：协议改好后模型明显变简短，事实准确率有波动——
把「标口范围 14-70mm」答成「24*24」、把「价格含税」答成「不含税」，
另有一条因引用了白名单外 id 被判 `HUMAN_REVIEW_REQUIRED`（这是拦截在正常工作）。

这不改变裁决结论（引用可追溯 ≠ 事实正确），但属于**已知限制**，已写入 SPEC，
下一步是提示词迭代 + 坏例回归，不是回退协议。

## 4. 断言覆盖（对应你 §4 的裁剪）

1. 协议测试组：清单 / 内联 / 混合去重 / 完全缺失 /「无 + 合法内联」——单测 6 项；
2. 展示：`test_citation_list_protocol_uses_system_side_locator`（末尾清单被剥离、locator 取系统侧）、
   `test_inline_marker_is_rendered_as_readable_index`（`[1]` 且不含内部 id）；
3. 放宽后的行为：`test_unmarked_sentences_no_longer_block_answer`（未标注句子不再拒答）；
4. 伪造：`test_forged_id_in_citation_list_still_requires_review` +
   既有内联伪造用例；
5. 拒答组：`test_citation_list_none_is_refused`、剥离后正文为空 → `INSUFFICIENT_EVIDENCE`；
6. 回归：无证据不调模型、结构化通道、RAG 其它用例全部保留。

## 5. 门禁

```
uv run pytest -q                      → 473 passed, 33.30s
uv run pytest -q -m requires_pg       → 297 passed, 176 deselected, 0 skip
uv run ruff check src tests migrations → All checks passed
uv run mypy                           → Success: no issues found in 62 source files
```

## 6. 已知限制（照实写）

1. 引用可追溯 ≠ 事实正确：覆盖率已是诊断而非闸门，事实错误需人工抽检与坏例回归；
2. 14B 在"满足格式"后倾向简短回答，准确率波动（实测 4/6）；
3. 协议冲突（写了"无"但正文有内联 id）目前只记日志，无持久化字段；
4. `引用：无` 与"引用行缺失"在审计里都落 `INSUFFICIENT_EVIDENCE`，仅日志可区分。

请裁决：**PASS** / **CHANGES_REQUESTED（逐条）**。
