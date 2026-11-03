# L1 送审稿：词法检索 + ACL 前置（`POST /v1/knowledge/search`）

- 日期：2026-11-03
- 提出方：L2 实施
- 风险归类命中：**鉴权/权限（检索层越权与信息泄露）**、**对外 API 契约变化**
- 请只做判断：**不要探索仓库、不要读其他文件、不要跑测试**。事实已由 L2 取证。

## 1. 范围

本轮只做**词法检索**（PostgreSQL `tsvector` + 中文分词适配器），不做向量检索、不接 LLM。
理由：ADR-04（模型供应商/跨境传输）与 ADR-06（硬件/SLO）未签署，稠密检索与生成无法定参；
`lexeme` 列、GIN 索引、中文分词适配器、状态过滤所需字段均已就绪。

当前 `POST /v1/knowledge/search` 返回 501。

## 2. 事实（已取证）

**表**：
```sql
knowledge_chunks(id, tenant_id, version_id, parent_chunk_id, chunk_kind, chunk_text,
                 chunk_sha256, tokenizer_id, heading_path, locator, acl_policy_id,
                 index_generation, lexeme tsvector, metadata)
  -- GIN 索引：idx_chunks_lexeme USING gin (lexeme)

knowledge_versions(id, tenant_id, document_id, status, classification, audience,
                   required_scope /* 0007 新增，NULL=未确认 */)

acl_policies(id, tenant_id, policy_json)
```

**分词约定**（`src/fkh/domain/lexeme.py`）：CJK 字间插空格 + `to_tsvector('simple', …)`；
查询侧统一 `plainto_tsquery('simple', segment_cjk(query))`，**不手拼 tsquery**（`& ! ( )` 会破语法）。
`TOKENIZER_ID = 'cjk-char-v1'` 写在 `knowledge_chunks.tokenizer_id`。
字级索引的已知代价：查询「撤权」会被拆成「撤」「权」两个 AND 词，命中「撤销权限」这类组合。

**ACL 求值器**（`src/fkh/authz/policy.py`）：Deny 优先 / 无匹配拒绝 / 策略缺失拒绝 / 跨租户拒绝，
四个维度 roles/groups/departments/subjects，无 `*` 通配符。令牌主体经
`PrincipalContext.to_subject()` 提供四个维度。

**状态与范围**（0007 后）：证据可取状态 = `APPROVED_INTERNAL`（audience=internal）与
`PUBLISHED_PUBLIC`（audience=customer_public）；`required_scope ∈ {internal_product,
internal_training, customer_public}`，`NULL` 时证据端点一律拒绝。

**既有审计表**：`rag_queries(id, tenant_id, principal_id, query_hash, scope, status,
refusal_code, trace_id, model_id, created_at)`，`status` CHECK 仅允许
`ANSWERED/REFUSED/HUMAN_REVIEW_REQUIRED/ERROR`，注释明确「仅存哈希，禁止落原始 query 明文」。

## 3. 拟议行为（待判）

**请求**（`knowledge.search` scope）：
```json
{"query": "...", "knowledge_scope": "internal_product", "max_results": 10}
```
`query` 长度 1–2000；`max_results` 1–50，默认 10。

**候选筛选（顺序固定）**：
1. SQL：`tenant_id = 当前租户` **且** 版本状态 ∈ {APPROVED_INTERNAL, PUBLISHED_PUBLIC}
   **且** `version.required_scope = :knowledge_scope` **且** `lexeme @@ plainto_tsquery(...)`；
2. 排序 `ts_rank_cd`，取 `max_results × 4`（上限 200）条候选；
3. 应用层逐条 ACL 判定（`evaluate(policy, subject, resource_tenant_id=...)`）；
4. 过滤后截断到 `max_results`。

**响应**（每条结果）：
```json
{"chunk_id","version_id","knowledge_id","text_snippet","locator","heading_path","score"}
```
外加 `{"results":[...],"index_generation":N,"trace_id":"..."}`。
`text_snippet` 截断到 400 字符，**不给全文**：全文由已裁决的 `GET /v1/evidence/{id}` 提供
（那里有重新鉴权与访问审计）。`score` 返回原始 `ts_rank_cd` 值并标注 `scoring="lexical-v1"`。

**不记录 `rag_queries`**：搜索不是问答，`status` 枚举里没有合适取值；本切片不写审计表。

## 4. 逐条待判问题

1. **「先放大 top-K 再 ACL 过滤」是否可接受？** 放大倍数取 4（上限 200）：
   若某租户大量内容对当前主体不可见，返回条数会少于 `max_results`。
   替代方案是把 ACL 下推成 SQL 条件——但 Deny/Allow 是多维集合运算，SQL 侧重复实现会与
   应用层求值器出现两套语义，我不建议。请裁决取哪种。
2. **检索是否做父块恢复？** 我倾向**不做**：命中 child 只返回该 child 的片段，
   需要全文时走证据端点（那里已强制「全部 child 可见才可恢复 parent」）。
   检索层再实现一次父块恢复，等于把同一条 P0 规则复制成两份。
3. **无结果与全部无权是否可区分？** 我倾向统一 `200 + 空列表`，
   理由：检索结果集本来就随查询变化，403 与空结果组合仍可被用来试探单条内容是否存在；
   但代价是调用方无法区分「没有相关内容」与「有但你没权限」。
4. **`knowledge_scope` 与 `required_scope` 的关系**：我按**精确匹配**实现
   （请求 `internal_product` 只读 `required_scope='internal_product'` 的版本）。
   是否需要「上级 scope 覆盖下级」（例如 `internal_product` 令牌可读 `internal_training`）？
5. **是否记录检索审计**？若不写 `rag_queries`，本切片对检索行为完全没有留痕。
   是否需要一个 `retrieval_queries`/`search_access_log`（含 query_hash、命中数、trace_id、
   是否发生全量被过滤）？如果要，本切片可用 0008；不要则可延后。
6. **`score` 是否对外暴露？** 暴露原始 `ts_rank_cd` 便于前端排序与调试，
   也可能被用来推断「有内容但没给我看」。是否改为不返回 score、只保证顺序？
7. **`text_snippet` 截断 400 字符**是否合适？是否需要按 token 而不是字符？
8. **查询串不得落日志**（L1 §6.3）。本切片日志只打 `trace_id`、命中数、耗时；
   是否还要求「即使异常也不得把 query 带进堆栈信息」？若是，需要额外的出参脱敏。

## 5. 固定回复骨架

```
【判决】1..8 逐条：采纳 / 改法
【必须补的测试】按"能失败的断言"描述，4~8 条
【响应字段最终清单】
【是否新增审计表】是/否 + 列清单
【可延后】
【风险提示】最容易上线后咬人的一条
```

## 6. 边界与预算

- 只做判断，不写实现、不跑测试、不要探索仓库。
- 本切片 L1 调用预算 1–2 次；落实登记进 `docs/SPEC/retrieval-lexical.md`。
