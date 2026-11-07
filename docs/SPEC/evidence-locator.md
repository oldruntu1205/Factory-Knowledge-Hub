# 受控证据定位（`GET /v1/evidence/{evidence_id}`）

> 依据：L1 §6.2 + 2026-11-03 L1 判决（`_evidence/l1_reply_evidence_20261103.md`）。
> 代码：`src/fkh/domain/evidence.py`（判定）、`src/fkh/api/routes_evidence.py`（HTTP 边界）；
> schema：`migrations/0007_evidence_access.sql`；契约：`contracts/schema/evidence_response.json`。

## 1. 这个端点解决什么

检索与问答（Phase 2）会给用户返回 citations。citations 里只有一个 `evidence_id`，
用户点「查看原文」时需要一个**重新鉴权**的入口，而不是拿一个永久有效的下载链接。
本端点就是这个入口：每次访问都重新走租户 → 状态 → scope → ACL 判定，并写访问审计。

`evidence_id` 的取值域是 `knowledge_chunks.id`（与 `AnswerResponse.citations[].evidence_id` 同源）。

**父块 / 子块都能查，但语义不同**（2026-11-07 补，集成方最容易误解的一处）：

| 传入的 id | 返回正文 | `text_evidence_id` |
|---|---|---|
| 子块 id | 父块全文（`text_level=PARENT`），且父块与其下全部子块都通过检查 | 父块 id |
| 父块 id | 父块全文，同样要求其下全部子块通过检查（第 5 条检查） | 父块 id |

而 **`AnswerResponse.citations[].evidence_id` 给的是「上下文块 id」**——父块恢复发生时它就是父块 id，
不是检索命中的子块 id。两者都可用于调用本端点，但前端展示的定位信息应与 `text_level` 一致地理解。
**不**接受 `rag_citations.id`：本端点定位可复用片段，不承诺引用级失效；历史 citation 不构成访问凭证。

## 2. 判定顺序（任一条不满足 ⇒ 403，对外不可区分）

| # | 检查 | 拒绝原因（仅审计可见） |
|---|---|---|
| 1 | `chunk.tenant_id == 令牌 tenant_id` | `RESOURCE_UNAVAILABLE` |
| 2 | 状态与 audience 组合合法：`APPROVED_INTERNAL+internal` 或 `PUBLISHED_PUBLIC+customer_public` | `STATE_NOT_VISIBLE` |
| 3 | `version.required_scope` 已赋值、取值合法、且被令牌 `allowed_scopes` 包含 | `SCOPE_NOT_ALLOWED` |
| 4 | 片段 ACL 允许（Deny 优先、无匹配拒绝、策略缺失拒绝） | `ACL_DENIED` |
| 5 | 返回 parent 全文时：parent 与其下**全部** child 通过同一组检查 | `PARENT_INCOMPLETE` |

第 1 条不依赖 RLS 兜底：**查询本身带 `tenant_id` 条件**，未命中就记 `RESOURCE_UNAVAILABLE`，
不额外去别的租户探测「这个 id 是否存在」——那本身就是一次越权探测。

第 5 条覆盖**直接按 parent id 请求**的路径：父块全文不能因为「请求的是 parent 本身」就跳过子块检查。

## 3. 响应

字段见 `contracts/schema/evidence_response.json`，全部 required：

`evidence_id, knowledge_id, version_id, status, classification, audience, locator, heading_path,
text, text_level, text_evidence_id, signed_url, signed_url_state, trace_id`

- `text_level=PARENT` 时 `text_evidence_id` 是父块 id，`evidence_id` 仍是请求的那条；
- `locator` / `heading_path` 对应**实际正文来源**，不是请求的那条；
- `locator` 只保留白名单字段（`block_index / page_start / page_end / paragraph_index /
  table_index / row_index / bbox`），白名单之外一律丢弃——未来新增字段若含存储路径，
  默认被丢掉而不是默认泄露；
- 不返回 `object_key` / 存储路径 / `chunk_sha256`。

**签名链接**：ADR-01（对象存储与区域）未签署，本版本固定 `signed_url=null`、
`signed_url_state="NOT_CONFIGURED"`，HTTP 仍 200。这是对原契约「返回短期签名链接」的
**显式修订**，已同步 `contracts/openapi/openapi.yaml` 与响应 schema。

## 4. 访问审计

`evidence_access_log`（0007 新增，强制 RLS，追加式）：

| 列 | 说明 |
|---|---|
| `tenant_id` / `principal_id` | 认证身份，取自令牌 |
| `evidence_id` | 请求的 id；**不设外键**，拒绝记录对应不存在的 id 也要能落 |
| `text_evidence_id` | 实际提供正文的块（ALLOWED 时） |
| `result` / `reason` | `ALLOWED`+`AUTHORIZED` 或 `DENIED`+拒绝原因 |
| `trace_id` | 与响应头 `X-Trace-Id` 同值 |
| `occurred_at` | 服务端时间 |

约束：
- 不含正文、签名 URL、存储路径；
- 成功路径**先落审计再返回正文**（服务内显式 commit）；审计写失败 ⇒ 500，不返回正文；
- 拒绝路径同样落审计，且在抛异常前提交（否则异常会把审计一起回滚掉）；
- 表是追加式的：UPDATE/DELETE 由触发器拒绝，只能追加新结论。

## 5. required_scope：为什么不能从 audience 反推

`audience='internal'` 只说明「不对外」，说明不了该给内部产品团队还是新人培训看。
从 audience 推 `internal_product` 会让培训材料被产品侧账号读到。
因此 0007 新增 `knowledge_versions.required_scope`：

- 取值 `internal_product / internal_training / customer_public`；
- 组合约束：`internal` → 前两者之一，`customer_public` → 仅 `customer_public`；
- **无默认值**：`NULL` 表示业务尚未确认，证据定位一律拒绝；
- 存量数据必须由业务逐条确认，不允许脚本批量填一个「看起来合理」的值。

上线前动作：`SELECT id, title FROM knowledge_versions WHERE required_scope IS NULL;` 逐条赋值。
未赋值的版本在证据定位上表现为 403（与越权不可区分），这是有意的 fail-closed。

## 6. 已知限制与残余风险

1. 签名 URL、有效期与「过期后不可读」验证未做（待 ADR-01 与对象存储方案）。
2. 证据定位不取代索引侧过滤：检索阶段仍必须按状态与 ACL 过滤候选，
   本端点是「拿到 id 之后的最后一道闸」，不能作为唯一防线。
3. 访问审计未接入告警；异常高频的 DENIED 需要人工或监控发现。
4. 引用级独立失效（某个 citation 失效但片段仍有效）未做。
5. 大文本未分页：parent 全文一次返回，超长父块需在检索层控制父块大小。

## 7. L1 判决落实登记（2026-11-03）

| 判决 | 落实 |
|---|---|
| 1. `evidence_id = chunk id`，每次重新鉴权 | `EvidenceService.resolve()`；路由每次调用重新判定 |
| 2. 五种拒绝同码同 body，审计区分原因 | `DenyReason` 只进审计；`test_all_denials_are_indistinguishable` |
| 3. 仅 `APPROVED_INTERNAL` / `PUBLISHED_PUBLIC`，拒绝 `SUPERSEDED` | `VISIBLE_COMBINATIONS` |
| 4. 新增 `required_scope`，不设默认值 | 0007 迁移 + `ALLOWED_REQUIRED_SCOPES` |
| 4'. parent 恢复须全 child 通过，直接请求 parent 同样受限 | `_family_readable()`；`test_direct_parent_request_cannot_bypass_child_checks` |
| 5. 200 + `signed_url:null` + `NOT_CONFIGURED` | 路由返回值 + schema + openapi 修订 |
| 6. 访问审计本切片做 | `evidence_access_log` + 成功/拒绝双写 + 追加式 |
| 响应字段最终清单 | `contracts/schema/evidence_response.json` |

未采纳或偏离：无。判决中「分组/部门授权需要补解析时继续用现有 ACL 求值器」一项，
已由 `PrincipalContext.to_subject()` 提供（另一条 lane 的改动，本切片直接复用）。
