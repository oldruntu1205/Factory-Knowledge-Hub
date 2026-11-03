# L1 送审稿：受控证据定位 `GET /v1/evidence/{evidence_id}`

- 日期：2026-11-03
- 提出方：L2 实施
- 风险归类命中：**鉴权/权限（越权与信息泄露）**、**对外 API 契约变化**
- 请只做判断：**不要探索仓库、不要读其他文件、不要跑测试**。事实与行号已由 L2 取证。

## 1. 目标

L1 §6.2 有 `GET /v1/evidence/{id}`（权限 `evidence.read`），契约描述只有一句：
「返回短期签名链接，不暴露私有存储路径；链接失效后不得再可读」。当前端点返回 501。
本切片实现「定位 + 受控取文」，签名链接受 ADR-01（对象存储与区域）未签署阻塞。

## 2. 事实（已取证）

**表**（`migrations/0001_core_schema.sql`）：

```sql
knowledge_chunks(id, tenant_id, version_id, parent_chunk_id, chunk_kind, chunk_text,
                 chunk_sha256, tokenizer_id, heading_path, locator jsonb, metadata jsonb,
                 acl_policy_id, index_generation, ...)
  FOREIGN KEY (tenant_id, version_id) REFERENCES knowledge_versions(tenant_id,id)
  FOREIGN KEY (tenant_id, acl_policy_id) REFERENCES acl_policies(tenant_id,id)

knowledge_versions(id, tenant_id, document_id, version_no, status, classification 0..4,
                   audience IN ('internal','customer_public'), approved_by, public_approved_by, ...)

acl_policies(id, tenant_id, policy_json jsonb)   -- {"allow":{roles,groups,departments,subjects},"deny":{...}}

rag_citations(id, tenant_id, query_id, chunk_id, version_id, locator jsonb)
```

**应用层 ACL**（`src/fkh/authz/policy.py`）：`evaluate(policy, subject, resource_tenant_id=...)`
→ Deny 优先 / 无匹配拒绝 / 策略缺失拒绝 / 跨租户拒绝；无 `*` 通配符。
`Subject` 维度 = principal_id / tenant_id / roles / groups / departments。

**父块恢复**（`src/fkh/authz/parent.py`）：`plan_restore()` 只允许在「parent 自身可见 且 **全部** child 可见」
时整段恢复 parent；缺 child 清单就降级为 child 文本。这是 L1 §8.4 的 P0。

**身份上下文**（`src/fkh/api/security.py`）：`PrincipalContext` 现有字段 = tenant_id / principal_id /
subject / roles / scopes / allowed_scopes；**没有 groups / departments**（令牌里可能有，但当前未解析）。

**契约响应示例**（`contracts/schema/answer_response.json`）：citations 项 =
`{evidence_id, knowledge_id, version_id, locator}`，`additionalProperties:false`。

**现有权限错误口径**（`src/fkh/api/routes_review.py`）：跨租户与「真的不存在」一律 403，
不允许用 404 探测资源。

**环境**：无对象存储、无 S3 凭据；ADR-01（私有域/对象存储/备份位置）UNSIGNED。

## 3. 拟议行为（待判）

1. `evidence_id` 取值域 = `knowledge_chunks.id`（与 citations.evidence_id 同源）；
   不接受 `rag_citations.id`，避免一个端点两种 ID 语义。
2. 可见性三重判定，任一不满足即 **403**（与「不存在」不可区分）：
   - 租户一致（不依赖 RLS 兜底）：`chunk.tenant_id == token.tenant_id`；
   - 版本状态 ∈ {`APPROVED_INTERNAL`, `PUBLISHED_PUBLIC`}（`RAW`/`DRAFT`/`REVIEWED`/`SUPERSEDED`/`REVOKED` 一律不可取）；
   - 片段 ACL allow 且非 deny（`evaluate(chunk_policy, subject, resource_tenant_id=...)`）。
3. audience → scope 映射：`version.audience='customer_public'` 要求令牌 `allowed_scopes` 含
   `customer_public`；`internal` 要求含 `internal_product`。不含即 403。
4. 文本级别：若 `chunk_kind='child'` 且其 parent 下**全部** child 可见，则返回 parent 全文并标
   `text_level=PARENT`；否则只返回该 child 文本并标 `text_level=CHILD`（复用 `plan_restore` 规则）。
5. 响应字段：`evidence_id, knowledge_id(=document_id), version_id, status, classification,
   audience, locator, heading_path, text, text_level, signed_url, signed_url_state, trace_id`。
   **不返回 `object_key` / 存储路径 / `chunk_sha256`。**
6. 签名链接：ADR-01 未签、无对象存储 ⇒ `signed_url: null`、`signed_url_state: "NOT_CONFIGURED"`，
   HTTP 仍 200（定位能力可用）。**不编造 URL、不降级暴露存储 key、不返回 501 掩盖可用的定位能力。**

## 4. 逐条待判问题

1. `evidence_id` 用 `chunk_id` 是否正确？若认为应改用 `rag_citations.id`（每次引用可独立失效），
   请直接否决并说明——两者的差别是「引用级失效」vs「片段级复用」。
2. 403 统一口径（不存在 / 跨租户 / 状态不可见 / ACL 拒绝 全部同码同 body）是否接受？
   还是要求「ACL 拒绝」与「不存在」在审计上可区分（对外仍同码）？
3. 版本状态白名单是否只保留 `APPROVED_INTERNAL` 与 `PUBLISHED_PUBLIC`？
   `SUPERSEDED`（被新版本取代但历史上批准过）是否允许取证据？我的倾向：**不允许**，
   因为引用它的问答应已随切换失效。
4. audience→scope 的映射（`internal` → `internal_product`）是否成立？
   是否存在「内部培训」内容的 evidence 需要 `internal_training` scope 才可取？
   若需要按 `version.audience` 之外的信息决定，当前 schema 里没有该字段，请指出应如何判定。
5. 签名链接不可用时的形态（200 + `signed_url_state=NOT_CONFIGURED`）是否可接受？
   还是要求 501 / 或要求本切片阻塞在 ADR-01？
6. **访问审计**：是否需要记录「谁在何时取了哪条证据」？
   若需要，现有表没有合适落点（`rag_queries` 是问答审计），需新增 `evidence_access_log`（含 RLS）。
   请裁决：本切片做 / 延后 / 不做。
7. `classification`（C0–C4）在响应中是否应原样返回？我倾向返回——调用方需要知道内容级别；
   但若 C 级本身属于敏感元数据，请要求降级为「不低于 C1」这类粗粒度。

## 5. 固定回复骨架

```
【判决】1..7 逐条：采纳 / 改法（给出替换语义或 SQL）
【必须补的测试】按"能失败的断言"描述，4~8 条
【响应字段最终清单】required + 允许为 null 的字段
【可延后】
【风险提示】最容易在上线后咬人的一条
```

## 6. 边界与预算

- 只做判断，不写实现、不跑测试、不要探索仓库。
- 本切片 L1 调用预算 1–2 次。采纳情况登记进 `docs/SPEC/evidence-locator.md`。
