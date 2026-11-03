# 词法检索（`POST /v1/knowledge/search`）

> 依据：L1 §4.3–4.4 + 2026-11-03 L1 判决（`_evidence/l1_reply_search_20261103.md`）。
> 代码：`src/fkh/retrieval/lexical.py`（召回 + ACL）、`src/fkh/retrieval/search.py`（范围授权 + 审计）、
> `src/fkh/api/routes_search.py`（HTTP 边界）；迁移：`0008_retrieval_queries.sql`。

## 1. 本轮范围

只做**词法检索**：`cjk-char-v1` 分词 → `lexeme`（tsvector）→ `ts_rank_cd` 排序 → ACL 过滤。
不做向量检索、不做 rerank、不接 LLM：ADR-04（模型供应商/跨境）与 ADR-06（硬件/SLO）未签署，
稠密检索无法定参；`lexeme` 列与 GIN 索引在 Phase 1 已就绪。

## 2. 两段式过滤

```text
SQL（硬条件）：tenant_id = 当前租户
              AND chunk_kind = 'child'          ← parent 不是普通命中
              AND tokenizer_id = 'cjk-char-v1'  ← 分词器必须与写入一致
              AND lexeme @@ plainto_tsquery('simple', segment_cjk(query))
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
 "scoring":"lexical-v1","trace_id":"..."}
```

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
| `trace_id` / `duration_ms` / `created_at` | 关联与耗时 |

- 不存 query 明文、片段、候选数或「全部被过滤」标志；
- 成功路径**先提交审计再返回结果**；审计写失败 ⇒ 500，且响应内不含结果与片段；
- 拒绝路径同样留痕（`SCOPE_NOT_AUTHORIZED`）；
- 执行失败留 `ERROR` + `EXECUTION_FAILED`，`returned_count=NULL`；
- 审计读取权限不随 `knowledge.search` 授予，需单独授权（读取端点本轮未做）。

配置：`FKH_RETRIEVAL_HASH_KEY`（必填，缺失则端点 503，不落可反推哈希）、
`FKH_RETRIEVAL_HASH_KEY_ID`（默认 `k1`）。

## 6. 分词语义（必须与索引一致）

- 写入与查询都用 `cjk-char-v1`：CJK 字符间插空格 + `to_tsvector('simple', …)`；
- 查询侧统一 `plainto_tsquery`，**不手拼 tsquery**：`& | ! ( ) :` 会被当作普通字符处理；
- 字级 AND 的代价：查询「撤权」= 撤 AND 权，会命中「撤销权限」；
- 换分词器必须重建 `lexeme` 并同步 `tokenizer_id`，否则永远匹配不上（本轮 SQL 已固定该值）。

## 7. 已知限制与残余风险

1. **候选挤占**（L1 点名最危险）：大量无权内容可能把有权内容挤出前 200，表现为漏检；
   这是本方案的固有代价，不做 ACL 下推不解决。
2. 无向量检索、无 rerank：召回质量受字级 AND 限制，同义词与词序不敏感。
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
