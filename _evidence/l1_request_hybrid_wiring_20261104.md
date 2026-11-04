# L1 送审稿：混合检索接入 `POST /v1/knowledge/search`（对外契约变更）

- 日期：2026-11-04
- 提出方：L2 实施
- 风险归类命中：**对外 API 契约变化**、**鉴权/权限（检索越权面）**、**审计可追溯（不改 schema 则无法区分召回模式）**
- 请只做判断：**不要探索仓库、不要读其他文件、不要跑测试**。下面的事实均已由 L2 取证。

## 1. 现状（已取证，2026-11-04 12:4x）

**已实现但未接线**（commit `827ba1a`，门禁 339 passed / DB 专项 212 passed 0 skip）：

- `retrieval/embedding.py`：`Embedder` 协议（`model_id`、`dimension`、`embed_documents`、`embed_query`）；`BGEM3Embedder`（`BAAI/bge-m3`，1024 维，MIT，本地权重、无任何远程 endpoint 配置，torch 为可选依赖，默认安装不含）；`DeterministicTestEmbedder`（哈希伪向量，仅测试）。
- `retrieval/hybrid.py`：`DenseRetriever`（SQL 只过滤 `tenant_id` / `required_scope` / 版本状态+audience / `embedding_model_id`，余弦排序，`LIMIT = min(max_results×4, 200)`）；`HybridRetriever`（向量路 + 词法路 → RRF `k=60` 只按排名融合 → 融合后对向量侧命中逐条做应用层 ACL 判定 → 去重、截断到 `max_results`）；`reciprocal_rank_fusion` 为纯函数。
- 向量写入与切片同事务（`workers/ingestion.py` + `workers/repository.py`），未配 embedder 时不写向量也不写假 `embedding_model_id`。
- 词法侧与向量侧共用同一份可见性谓词常量 `VISIBILITY_PREDICATE`（租户/范围/状态），避免两路口径漂移。

**接线现状（关键）**：

- `retrieval/search.py` 的 `SearchService.search()` 仍只调用 `LexicalRetriever`。
- `POST /v1/knowledge/search`（`api/routes_search.py`）与 `POST /v1/knowledge/context`（`api/routes_context.py`）**都经过 `SearchService`**，因此接线会同时改变两个端点的召回来源。
- 响应 `scoring` 当前直接取 `lexical.SCORING = "lexical-v1"`。

**契约现状**：

- `contracts/schema/search_response.json`：`"scoring": { "type": "string", "const": "lexical-v1" }`。
- `contracts/openapi/openapi.yaml` 搜索端点描述：「词法检索（cjk-char-v1 分词 + ts_rank_cd 排序），**不做向量检索与生成**。max_results 是返回上限，不保证填满……空结果与「都无权限」不可区分」。
- 现有端到端断言只断言响应键集合 `{results, scoring, trace_id}`，**没有**断言 `scoring` 的具体取值；契约文件与运行时代码靠 `tests/contract` 比对 openapi，不做响应值兜底。也就是说：改运行时不改契约，测试不会红（这是当前的真实风险点）。

**审计表** `retrieval_queries` 现有列：`tenant_id, principal_id, query_hash, query_hash_key_id, knowledge_scope, status, refusal_code, returned_count, trace_id, duration_ms`。**没有「召回模式」列**。既有判决要求：query 只落 HMAC 摘要；不记录候选数与「全部被过滤」标志（可被用来推断无权内容存在性）；成功路径先提交审计再返回。

**配置**：`embedding_provider ∈ {none, bge-m3, test}`（默认 `none`）、`embedding_device`（默认 `cpu`）、`embedding_batch_size`。真实依赖（torch / sentence-transformers）**尚未安装**，真实 BGE-M3 未跑过，真实语料 recall 未测（见 `docs/SPEC/embedding-and-hybrid.md` §6–7）。

**已裁决的相关前提**：ADR-04/06 仍 `UNSIGNED`；此前判决为「不做 ACL 下推（细粒度判定留在应用层）」「检索写入 `retrieval_queries` 审计」「公开可见性= `APPROVED_INTERNAL/internal` 或 `PUBLISHED_PUBLIC/customer_public`」「撤销立即生效，检索侧按状态过滤」。

## 2. 拟议改动（待判）

1. `SearchService` 增加可选 `embedder`；`provider=none` 时行为完全不变（纯词法）。
2. 启用后：向量+词法双路召回、RRF 融合、融合后 ACL 不变式保持，`scoring` 返回 `hybrid-rrf-v1`。
3. 审计表本轮不新增列（先按现状），需要的话再单开迁移。

## 3. 待判问题（请逐条裁决 + 一句理由 + 需要的断言）

- **Q1 `scoring` 契约**：A 把 `const "lexical-v1"` 改成 `enum ["lexical-v1","hybrid-rrf-v1"]`；B 保持 const 不变、改用请求参数/服务端开关但响应仍写 `lexical-v1`（会与实现不符）；C 新增 `/v2` 端点保 v1 语义。选哪个？破坏面如何界定（调用方按 const 校验会硬失败）？
- **Q2 默认开关与缺模型行为**：混合检索默认开启，还是 opt-in（请求参数或服务端开关）？`provider=none`（未装模型/未配置）时应 **fail-open 退化为纯词法并返回 `lexical-v1`**，还是 **fail-closed 返回 503**？请给判据（「不假装有能力」vs「不因缺模型否定已有词法能力」）。
- **Q3 审计可追溯**：一次检索是词法还是混合，是否必须落审计？若必须，只能新增列（当前最大迁移号 0009；**0010 已被「草稿终止」方案登记占号**，新迁移应取 0011）。「记录召回模式」是否与既有「不记录候选数/过滤标志」的判决冲突？
- **Q4 越权候选的暴露面**：向量路 SQL 只做租户/范围/状态三类过滤，细粒度 ACL 在**融合之后**于应用层判定（即内存候选集里会短暂包含无权 chunk，绝不返回、不落库、不进审计）。在「不做 ACL 下推」的既有判决下这是否可接受？是否需要额外的越权计数/日志/告警，或必须改为 SQL 前置过滤（那会与既有判决冲突）？
- **Q5 两路深度不对称**：向量路取 `max_results×4`（≤200）、词法路取 `max_results`，RRF 只按排名融合。是否要求两路同深度再融合？这会改变候选集语义与返回条数分布，是否与契约里「max_results 是上限、不保证填满」的表述冲突？
- **Q6 契约与断言的改动授权**：本切片必须同时改 `contracts/openapi/openapi.yaml`、`contracts/schema/search_response.json` 与相关断言（含 `tests/contract`）。按「契约强制变更例外」授权 L2 一并改并**随切片送终审**，还是先由 L1 给出断言、L2 只实现？
- **Q7 `/v1/knowledge/context` 连带影响**：它同样走 `SearchService`，接线后上下文装配的候选来源由「词法 top-N」变成「双路融合 top-N」。是否需要单独的拒答阈值/回归（`NO_EVIDENCE` / `INSUFFICIENT_EVIDENCE` / `HUMAN_REVIEW_REQUIRED` 三档语义是否保持不变）？请明确本切片的边界。

## 4. 边界与预算

- 只做判断，不探索仓库、不写代码、不跑测试；一次调用回完 Q1–Q7。
- 回复骨架：`Qn → 裁决（A/B/C/其他） + 一句理由 + 必须补的断言`。
- 若某项判定依赖尚未签署的 ADR，请直接标明「待 ADR 裁决」并给出可先行的安全取值（fail-closed 优先）。
