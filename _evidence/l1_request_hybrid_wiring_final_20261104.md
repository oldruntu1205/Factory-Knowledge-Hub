# L1 终审：混合检索接线切片（`e75f3c9`）

- 日期：2026-11-04
- 送审方：L2 实施
- 对应裁决：`_evidence/l1_reply_hybrid_wiring_20261104.md`（Q1–Q7）
- 风险归类：**对外 API 契约变化**、**鉴权/权限（检索越权面）**、**数据库迁移**
- 请只做判断：**不要探索仓库、不要读源码、不要跑测试**。证据已由 L2 取证并按 Q 编号列出。

## 1. 改了什么（相对上一提交 `30e41a9`）

| 文件 | 改动 |
|---|---|
| `contracts/schema/search_response.json` | `scoring` 由 `const "lexical-v1"` 改为 `enum ["lexical-v1","hybrid-rrf-v1"]`，描述写明取值集合扩大 |
| `contracts/openapi/openapi.yaml` | 搜索端点描述改为「默认词法 / 服务端可 opt-in 混合 / 缺模型 503 / 有意的破坏性契约变更」；context 端点描述写明固定词法（Q7） |
| `src/fkh/config.py` | 新增 `retrieval_mode: str = "lexical"`（服务端配置，非请求参数） |
| `src/fkh/retrieval/search.py` | `SearchService` 增加 `embedder`、`rrf_k`、`retrieval_mode`；返回 `SearchResult(hits, scoring)`；审计写入 `retrieval_mode` |
| `src/fkh/retrieval/embedding.py` | 新增 `EmbeddingError`（执行期故障）；`build_embedder(env=...)` 在生产拒绝 `test`；`get_embedder` 按配置缓存；`SentenceTransformer(..., local_files_only=True)` |
| `src/fkh/api/routes_search.py` | 只有 `hybrid` 才解析 embedder；`EmbeddingNotConfigured` → 503；响应 `scoring` 取实际执行模式 |
| `src/fkh/api/routes_context.py` | 显式传 `LEXICAL_MODE`（不跟随开关） |
| `src/fkh/api/main.py` | `/health/ready`：hybrid 配置下模型不可用 → 503 `embedding_unavailable`；未知模式 → 503 `invalid_mode` |
| `migrations/0011_retrieval_mode.sql` + `rollback/0011_down.sql` | `retrieval_queries.retrieval_mode`，历史按依据回填（执行过 → `lexical-v1`；召回前被拒 → `not_executed`），随后 `SET NOT NULL`、`DROP DEFAULT`、加枚举 CHECK |
| `tests/e2e/test_search_hybrid_api.py` | 新增 17 项（映射见 §3） |
| `docs/SPEC/retrieval-lexical.md` §9–§10、`embedding-and-hybrid.md` §7、`HANDOFF.md` §2/§5/§6/§7、`README.md`、`.env.example` | 文档与配置样例同步 |

## 2. Q1 要求的兼容影响与启用条件（终审材料）

**兼容影响**：`scoring` 是响应中**唯一**取值集合发生变化的字段。
- 按 `const "lexical-v1"` 做校验的调用方在启用 hybrid 后会**硬失败**；未启用 hybrid 时取值不变。
- 字段集合、错误结构、403/503 语义、`max_results` 上限均未变。
- 因此判为**有意的破坏性变更**，已在 openapi 描述与 schema description 中写明，
  并要求调用方在启用前放宽断言——不宣称向后兼容。

**启用条件（按 Q2）**：
1. `FKH_RETRIEVAL_MODE=hybrid`（默认 `lexical`）；
2. `FKH_EMBEDDING_PROVIDER=bge-m3` 且本机权重已就绪（可选依赖 `uv sync --extra embedding`）；
3. `/health/ready` 返回 200 且 `checks.retrieval=ok`；
4. **生产启用仍未授权**：真实 BGE-M3 推理与代表性语料 recall@10 尚未实测（Q2 明示）。

## 3. Q1–Q7「必须补的断言」与测试映射（17 项，全绿）

| 判决 | 断言 | 用例 |
|---|---|---|
| Q1 | 纯词法返回 `lexical-v1`；实际双路才返回 `hybrid-rrf-v1`；响应通过 JSON Schema；契约与实现一致 | `test_default_config_is_lexical_and_schema_valid`、`test_hybrid_executes_both_paths_and_reports_hybrid_scoring`、`test_success_audit_mode_matches_response` |
| Q2 | 默认词法；hybrid 缺模型/未知模式/生产用 test embedder 均不伪成功；纯词法不加载模型依赖 | `test_hybrid_without_model_is_503_and_audits_not_executed`、`test_unknown_mode_is_503_and_never_falls_back_to_lexical`、`test_production_env_rejects_test_embedder`、`test_lexical_mode_never_loads_embedding_dependencies`、`test_inference_failure_is_503_not_fake_success` |
| Q3 | 审计模式为有限枚举；成功响应与已提交审计一致；失败路径不伪记成功模式 | 三个审计断言用例（SUCCEEDED ↔ 响应 scoring；缺模型/未知模式 → `not_executed`；推理失败 → `hybrid-rrf-v1` + `EMBEDDING_UNAVAILABLE`） |
| Q4 | 无权块不进入结果；两路命中与重复命中外均不能绕过 ACL | `test_hybrid_applies_acl_after_fusion`、`test_hybrid_dedupes_and_respects_max_results`、`test_hybrid_hides_revoked_and_unapproved_versions` |
| Q5 | 向量上限 `min(max×4,200)`、词法保持原上限、RRF `k=60`、单路空仍可融合、去重后不超上限 | `test_dense_candidate_cap_follows_formula`、`test_rrf_default_k_is_sixty`、`test_hybrid_fuses_when_dense_side_is_empty`、`test_hybrid_dedupes_and_respects_max_results` |
| Q6 | 契约 + 实现 + 断言同一提交 | 本提交 `e75f3c9` |
| Q7 | context 在 search 启用 hybrid 时仍走词法，审计模式为 `lexical-v1` | `test_context_endpoint_stays_lexical_even_when_search_is_hybrid` |

补充：`test_audit_failure_blocks_hybrid_results`（审计写失败 ⇒ 响应 ≥500 且不含片段）、
`test_vectors_from_other_model_are_ignored`（换模型隔离）。

## 4. 实测证据（独家运行，无并发）

```
uv run pytest -q                      → 356 passed, 13.43s
uv run pytest -q -m requires_pg       → 229 passed, 127 deselected, 0 skip, 12.83s
uv run ruff check src tests migrations → All checks passed
uv run mypy                           → Success: no issues found in 50 source files
迁移演练（同一进程持有 pgserver）：0011 APPLIED → --rollback 1 → PENDING → 重新 APPLIED
```

## 5. 已知限制 / 需要裁决的残余项

1. **真实模型与 recall 未验证**：`DeterministicTestEmbedder` 只证明工程链路；BGE-M3 未安装、未跑语料评估。
2. **迁移号跳过 0010**（预留草稿终止）：`apply.py` 按文件名顺序应用，跳过不报错；
   但**回滚 2 步会因 0010 缺 down 脚本显式失败**——这是刻意行为，已写入 HANDOFF §5。
3. **0011 在迁移内临时关闭了 `retrieval_queries` 的追加式 UPDATE 触发器**做历史回填，
   语句结束立即恢复；运行期路径仍无 UPDATE 权限（既有 append-only 用例仍绿）。
4. **`scoring` 不含 hybrid 内部参数**（rrf_k、深度、模型 id）：沿用既有「不返回可推断性元数据」口径；
   若需要在响应中暴露模型版本以便前端标注，属新契约决策，未做。

请裁决：**PASS** / **CHANGES_REQUESTED（逐条列出必须改的点）**。
如认为某项属「待 ADR」而非本切片，请直接标明，我按 fail-closed 处理并登记。
