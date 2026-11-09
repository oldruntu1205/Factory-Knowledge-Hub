# 词法检索（`POST /v1/knowledge/search`）

> 依据：L1 §4.3–4.4 + 2026-11-03 L1 判决（`_evidence/l1_reply_search_20261103.md`）。
> 代码：`src/fkh/retrieval/lexical.py`（召回 + ACL）、`src/fkh/retrieval/search.py`（范围授权 + 审计）、
> `src/fkh/api/routes_search.py`（HTTP 边界）；迁移：`0008_retrieval_queries.sql`。

## 1. 本轮范围

只做**词法检索**：`cjk-char-v1` 分词 → `lexeme`（tsvector）→ `ts_rank_cd` 排序 → ACL 过滤。
不做向量检索、不做 rerank、不接 LLM：ADR-04（模型供应商/跨境）与 ADR-06（硬件/SLO）未签署，
稠密检索无法定参；`lexeme` 列与 GIN 索引在 Phase 1 已就绪。

> 2026-11-04 更新：在保持**默认词法**的前提下，新增服务端 opt-in 的混合模式（§9）。
> 契约 `scoring` 由 `const` 扩大为 `enum`，属**有意的破坏性变更**，见 §10 判决登记。

## 2. 两段式过滤

```text
SQL（硬条件）：tenant_id = 当前租户
              AND chunk_kind = 'child'          ← parent 不是普通命中
              AND tokenizer_id = 'cjk-char-v1'  ← 分词器必须与写入一致
              AND lexeme @@ websearch_to_tsquery('simple', build_query(query))
              AND version.required_scope = :knowledge_scope（精确匹配，NULL 不可见）
              AND (APPROVED_INTERNAL/internal) OR (PUBLISHED_PUBLIC/customer_public)
ORDER BY ts_rank_cd DESC, chunk_id LIMIT max_results × 4（上限 200）

应用层：逐条 evaluate(policy, subject, resource_tenant_id)  ← Deny 优先 / 无匹配拒绝 / 策略缺失拒绝
截断到 max_results
```

**为什么不把 ACL 下推成 SQL**：可见性是四维集合运算（allow/deny × roles/groups/departments/subjects），
SQL 侧再实现一遍必然与应用层求值器出现两套语义，一处改不同步就是越权或漏检。

**代价（已被 L1 明确接受）**：`max_results` 只是上限，不保证填满，也不保证拿到全部可见内容的
完整 top-K。无权内容可能把有权内容挤出候选窗口。因此：
- 契约里**不得承诺**「空结果说明没有可见相关内容」；
- 也不得承诺「完全隐藏无权内容的存在」——条数与耗时的侧信道无法完全消除。

## 3. 为什么 parent 不作为普通命中

parent 的文本聚合了它全部 child。即使不做「恢复」，直接返回 parent 片段也可能带出无权 child 的内容。
所以检索只返回 `chunk_kind='child'`；父块全文必须走 `GET /v1/evidence/{id}`，
那里强制「parent 与其下全部 child 均可见」才可恢复（见 `docs/SPEC/evidence-locator.md`）。

## 4. 响应

`contracts/schema/search_response.json`：

```json
{"results":[{"chunk_id","version_id","knowledge_id","text_snippet",
             "snippet_truncated","locator","heading_path"}],
 "scoring":"lexical-v1 | hybrid-rrf-v1","trace_id":"..."}
```

- `scoring` **如实标识本次实际执行的召回模式**（2026-11-04 判决 Q1）：纯词法 `lexical-v1`，
  真正走了双路融合才是 `hybrid-rrf-v1`；它由服务端配置决定，不是请求参数；

- `knowledge_id` = `knowledge_versions.document_id`；
- `text_snippet` ≤ 400 个 Unicode 码点，只取自已授权命中块，**不拼接父块或相邻块**；
- **不返回** `score`、`index_generation`、候选数、过滤统计——这些都能被用来推断无权内容；
- 空结果与「全部被 ACL 过滤」不可区分（同为 `200 + []`）；
  但**缺少接口权限**或**请求范围未授权**仍是 403，不能伪装成空结果。

## 5. 检索审计

`retrieval_queries`（0008，强制 RLS，追加式）：

| 列 | 说明 |
|---|---|
| `query_hash` | 服务端密钥的 **HMAC-SHA256**（普通哈希可被字典反推短查询） |
| `query_hash_key_id` | 轮换识别用；密钥本身不入表 |
| `knowledge_scope` / `status` / `refusal_code` | `SUCCEEDED`（含空结果）/ `REFUSED` / `ERROR` |
| `returned_count` | 返回条数；无法确定时 NULL，正常空结果为 0 |
| `retrieval_mode` | 实际执行的召回模式（0011）：`lexical-v1` / `hybrid-rrf-v1` / `not_executed` |
| `trace_id` / `duration_ms` / `created_at` | 关联与耗时 |

- `retrieval_mode` 记录**执行事实**：召回前就被拒绝（如范围未授权）写 `not_executed`，
  不得伪记成执行过某种模式；配置错误（未知模式、混合缺模型）同样写 `not_executed`；
  已经进入召回但推理失败写 `hybrid-rrf-v1` + `EMBEDDING_UNAVAILABLE`；

- 不存 query 明文、片段、候选数或「全部被过滤」标志；
- 成功路径**先提交审计再返回结果**；审计写失败 ⇒ 500，且响应内不含结果与片段；
- 拒绝路径同样留痕（`SCOPE_NOT_AUTHORIZED`）；
- 执行失败留 `ERROR` + `EXECUTION_FAILED`，`returned_count=NULL`；
- 审计读取权限不随 `knowledge.search` 授予，需单独授权（读取端点本轮未做）。

配置：`FKH_RETRIEVAL_HASH_KEY`（必填，缺失则端点 503，不落可反推哈希）、
`FKH_RETRIEVAL_HASH_KEY_ID`（默认 `k1`）。

## 6. 分词语义（必须与索引一致）

- 写入与查询都用 `cjk-char-v1`：CJK 字符间插空格 + `to_tsvector('simple', …)`；
- 查询侧统一 `websearch_to_tsquery('simple', build_query(query))`：**字级 OR** 组合、
  每个词项加双引号（裸 `-word` / `or` / 未闭合引号都不会改变语义）、**不手拼 tsquery**；
- **2026-11-04 修正**：此前用 `plainto_tsquery` 把每个汉字 AND 起来，自然语言问句几乎必然零命中
  （50 条测评题 recall@10 = 0.06）；改 OR 后同语料同评测集为 0.68，`scoring` 升级为 `lexical-v2`。
- 字级 AND 的代价：查询「撤权」= 撤 AND 权，会命中「撤销权限」；
- 换分词器必须重建 `lexeme` 并同步 `tokenizer_id`，否则永远匹配不上（本轮 SQL 已固定该值）。

## 7. 已知限制与残余风险

1. **候选挤占**（L1 点名最危险）：大量无权内容可能把有权内容挤出前 200，表现为漏检；
   这是本方案的固有代价，不做 ACL 下推不解决。
2. 默认无向量检索、无 rerank：召回质量受字级 AND 限制，同义词与词序不敏感。
   混合模式（§9）只能缓解，不能替代真实模型评估与 rerank。
3. 无分页：`max_results` 上限 50，深翻页未支持。
4. 无 `index_generation` 快照：材料尚未定义混合代次结果如何对应唯一 N，故本轮不返回该字段。
5. 审计读取端点、检索审计告警未做。
6. 查询串虽不落日志，但 HTTP 访问日志若由网关记录 URL/请求体，仍需在部署侧确认关闭 body 采集。

## 8. L1 判决落实登记（2026-11-03）

| 判决 | 落实 |
|---|---|
| 1. 应用层 ACL、4 倍候选、上限 200、`chunk_id` 稳定次序 | `OVERFETCH`/`MAX_CANDIDATES`/`ORDER BY … , c.id` |
| 2. 不做父块恢复，且 parent 不得作为普通命中 | SQL `chunk_kind='child'` |
| 3. 统一 `200 + []`，不回泄露统计 | 响应仅 `results/scoring/trace_id` |
| 4. 精确 scope，无继承；另校验令牌范围 | `SearchService` 的 `allowed_scopes` 校验 |
| 5. 新增 `retrieval_queries`（0008） | 迁移 + HMAC + 三类状态 |
| 6. 不返回 `score`，保留 `scoring` | 响应字段清单 |
| 7. 片段 ≤400 码点 + `snippet_truncated` | `lexical._snippet()` |
| 8. query 明文不进日志/异常/校验响应 | 422 统一文案（不再回显 `input`）、审计只存 HMAC |
| 附：只匹配 `cjk-char-v1` 块 | SQL `tokenizer_id` 条件 |
| 附：移除顶层 `index_generation` | 未实现 |

## 9. 混合模式（2026-11-04 接线）

```text
服务端开关 FKH_RETRIEVAL_MODE=lexical|hybrid（默认 lexical）
hybrid：向量路（pgvector 余弦，tenant/scope/状态/model_id 过滤，LIMIT=min(max×4,200)）
      + 词法路（§2 同款 SQL）
      → RRF(k=60) 只按排名融合
      → 融合后对向量侧命中逐条应用层 ACL（词法侧已在 SQL 后判定过）
      → 去重、截断到 max_results
```

**为什么用 RRF 而不是加权分数**：余弦相似度在 0–1、`ts_rank_cd` 无上界，直接相加必须各配
一个靠调出来的归一化参数；RRF 只用排名，不引入拍脑袋的权重。

**为什么融合后还要鉴权**：向量路 SQL 只做租户/范围/状态三类过滤，细粒度 ACL 是 JSONB 策略，
留在应用层判定。若融合后不判，就是「先召回后鉴权」。

**失败语义（判决 Q2）**：显式启用混合后，模型未安装、权重缺失、推理异常一律 503，
并留 `ERROR` 审计；**绝不静默降级**成词法结果——否则「以为开了混合」与「实际纯词法」
无法区分。纯词法路径不加载任何模型依赖。

**context 不跟随**（判决 Q7）：`/v1/knowledge/context` 固定词法。候选来源变化会改变
`NO_EVIDENCE / INSUFFICIENT_EVIDENCE / HUMAN_REVIEW_REQUIRED` 的分档，接混合须另行送审。

## 10. L1 判决落实登记（2026-11-04，`_evidence/l1_reply_hybrid_wiring_20261104.md`）

| 判决 | 落实 |
|---|---|
| Q1 `scoring` 改 enum，按破坏性变更管理 | `search_response.json` enum + openapi 描述明确"取值集合扩大" |
| Q2 默认词法、服务端 opt-in、缺模型 503 | `FKH_RETRIEVAL_MODE` 默认 `lexical`；`SearchService` 缺 embedder 即 503；`/health/ready` 同步 503 |
| Q3 审计记录实际模式，迁移 0011 | `retrieval_queries.retrieval_mode` 有限枚举 + 历史按依据回填（REFUSED → `not_executed`） |
| Q4 接受应用层 ACL，不加逐请求越权计数 | 融合后判定；单测覆盖两路命中与重复命中 |
| Q5 保留两路深度不对称 | 向量 `min(max×4,200)`、词法 `max`；测试固定该公式 |
| Q6 授权 L2 同步改契约与断言 | 契约 + `tests/e2e/test_search_hybrid_api.py`（17 项）+ 本切片终审 |
| Q7 context 保持词法 | `routes_context` 显式传 `LEXICAL_MODE`；测试断言审计模式与词法 scoring 一致 |

### 10.1 2026-11-04 追加判决：词法查询由 AND 改为 OR（裁决 A）

| 判决 | 落实 |
|---|---|
| A. OR + `ts_rank_cd`，不引入阈值、不改索引 | `domain/lexeme.build_query` + `websearch_to_tsquery`；迁移 0012 扩展审计枚举 |
| `scoring` 采用 `lexical-v2` | `lexical.SCORING` + 契约 enum（v1 为历史取值） |
| 空白/纯标点/无有效词项 → 空结果 | `build_query` 丢弃无字母数字的 token；单测 8 项 |
| 召回回归门槛（本评测集 ≥33/50） | 实测 34/50，原先命中的 3 条无退化（`recall-benchmark.md`） |
| 精度证据（P@K / 实际返回精度） | 已补：P@10 0.162（词法）/ 0.232（混合），文档级代理口径 |
| 精度下限 | **未定**，由 L1 在查看精度结果后设定；未达标前不批准生产发布 |

> **终审结论（2026-11-04）**：`_evidence/l1_reply_hybrid_wiring_final_20261104.md` 提出两项修改
> （迁移跳号回滚、回填原子性证据），落实于 `302c367` 后经 `_evidence/l1_reply_hybrid_wiring_rereview_20261104.md`
> 判定 **PASS**。生产启用混合检索仍未授权（真实 BGE-M3 与 recall@10 待验收）。


## 12. 排序 v3：BM25 字级重排（2026-11-06）

**问题**：字级 OR + `ts_rank_cd` 没有 IDF。客户目录里每页重复同一段"高硼硅精选管材……以下规格均为常用规格"，
高频字（的/管/规格）把这些样板段落顶上去，"试管"这样的判别词反而排不进来——实测「有哪些规格的试管？」
top-5 里一个试管块都没有。

**做法**：候选集不变（仍字级 OR + GIN），排序改为字级 BM25：

```
score = Σ_t idf(t)·tf·(k1+1) / (tf + k1·(1 - b + b·|d|/avg_len))
idf(t) = ln((N - df + 0.5)/(df + 0.5) + 1)，k1 = 1.2，b = 0.75（固定）
```

- `N/df/avg_len` 取**同一可见性范围的全部块**（租户 + scope + 状态 + **ACL**），
  不可见块不参与统计；
- 候选窗口 `min(OVERFETCH × max_results, 200)`，`max_results > 200` 显式拒绝；
  `SearchOutcome.window/truncated` 暴露截断诊断；
- 一次 SQL 往返完成统计；per-term 统计用 `plainto_tsquery`，兼容运算符输入；
- 审计 `retrieval_mode = lexical-v3`（迁移 0016），`lexical-v2` 保留为历史取值。

**实测**：冻结用例 recall@10 **0.48 → 0.62**（改善 8 条、退化 1 条 H05）；
5000 块规模检索中位 **142ms**（主要耗在全范围统计；10 万块级需要统计缓存/统计表，列为后续项）。
2026-11-09 补 10 万块基准：p50 **2.71s** / p95 **3.63s**（单查询、合成数据；候选 SQL 1.32s + 统计 1.43s），
证明只做统计缓存最多降到约 1.3s，候选排序也需优化；见 `_evidence/retrieval_perf_20261109.md`。

**已知退让**：标口范围类查询的名次从 2 降到 3（BM25 更看重判别词，牺牲少量"关键词密度型"排序）。

**终审：PASS**（`_evidence/l1_reply_lexical_final_20261106.md`）——统计总体一致性、H05 退化披露、
六项裁决落实均已确认；窗口截断、大租户统计成本、融合基准列为后续项。

## 外贸客服端到端验收（2026-11-07）

`tests/e2e/test_trade_service_e2e.py`（6 条，真实 PG + 四个身份 + HTTP）：

| 场景 | 断言 |
|---|---|
| 公开事实 | 客户问规格 -> ANSWERED，引用版本 = 已公开版本；内部成本不出现在答案里 |
| 商务承诺 | 单价/多少钱/交期/保证/定制/MOQ/英文 price+lead time -> 全部 HUMAN_REVIEW_REQUIRED，答案不含价格 |
| 撤销 | 撤销后问答不可答、检索为空、旧引用直读 403 |
| 审计 | rag_queries.principal_id = 客户主体、scope=customer_public、只存哈希（明文 0 命中） |
| 副作用 | 问答前后 outbox_events 计数不变（ADR-07 未签，不对外发送） |
| 跨域隔离 | 客户读文档管理面 403、用 internal_product 范围 403、读培训正文 403 |
