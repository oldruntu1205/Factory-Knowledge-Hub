【判决】

**有条件采纳；第 4 项的资源 scope 缺口和父块恢复权限必须补齐后才能上线。**

1. **采纳：`evidence_id = knowledge_chunks.id`。**  
   本端点定位可复用片段，不承诺引用级失效；不接受 `rag_citations.id`。每次访问重新鉴权，历史 citation 不构成访问凭证。入口仍须检查有效授权 `evidence.read`。

2. **采纳：资源不存在、跨租户、状态不可见、ACL 拒绝、scope 不符，对外统一 403、同一 body 结构。**  
   审计中须区分能够安全判定的拒绝原因；租户过滤查询未命中可记 `RESOURCE_UNAVAILABLE`，**不得为了区分不存在与跨租户而额外查询其他租户**。缺失策略、策略求值失败均拒绝。当前未解析的 groups / departments 按空集合处理，不能忽略 deny 或据此放行。

3. **采纳：仅允许 `APPROVED_INTERNAL`、`PUBLISHED_PUBLIC`，拒绝 `SUPERSEDED`。**  
   本切片同时限定合法组合为：
   - `APPROVED_INTERNAL` + `internal`
   - `PUBLISHED_PUBLIC` + `customer_public`

   其他组合拒绝。拒绝旧版本无需以“问答已完成失效”为前提；旧引用访问时仍必须重新检查。

4. **改法：不能从 `audience='internal'` 推导 `internal_product`。**  
   现有事实不足以判定内部产品与内部培训的归属。为版本新增明确的 `required_scope`，取值为 `internal_product / internal_training / customer_public`；约束：
   - `internal` 对应前两者之一；
   - `customer_public` 仅对应 `customer_public`；
   - 未赋值、未知值、令牌可信 `allowed_scopes` 不包含该值，均拒绝。

   存量数据须经业务确认赋值，不设 `internal_product` 默认值。分组或部门授权需要补解析时，应继续使用现有 ACL 求值器。

5. **采纳：200 + `signed_url:null` + `signed_url_state:"NOT_CONFIGURED"`。**  
   这是对原“返回签名链接”契约的**明确修订**，须同步 L1、接口 schema 和调用方预期；本切片不因 ADR-01 阻塞。签名链接交付仍延后。

   **第 4 条拟议行为另须修正：**恢复 parent 必须满足 parent 自身及完整 child 集合全部通过相同的租户、版本、scope、ACL 检查，并确认同租户、同版本。无法证明 child 清单完整时，只返回已授权 child。**直接请求 parent 也必须执行同一规则**，不能绕过子块检查。

6. **裁决：访问审计本切片做。**  
   新增含 RLS 的 `evidence_access_log`，至少记录：服务端时间、认证租户、principal、请求 evidence_id、实际文本来源 ID、结果、内部原因、trace_id。拒绝记录不得要求 evidence 外键存在；不记录正文、签名 URL 或存储路径。审计仅供授权审计角色访问。成功访问须在返回正文前可靠落审计，落审计失败不得返回正文。

7. **采纳：授权成功后原样返回 `classification` C0–C4。**  
   当前没有事实要求对已授权调用方隐藏级别，粗化也不能补救正文越权。**返回级别不等于完成级别授权**；若另有分级准入规则，仍须纳入访问判定。

【必须补的测试】

1. 缺少 `evidence.read` 时，即使资源 ACL 放行，也不能取得正文。
2. 不存在、跨租户、状态拒绝、ACL 拒绝、scope 拒绝的响应同为 403，body 除独立生成的 trace_id 外一致，均不含资源元数据。
3. deny 覆盖 allow；缺失策略、仅依靠未解析 groups / departments 的 allow 均不能放行。
4. `SUPERSEDED` 等非白名单状态，以及状态与 audience 错配，均不能取得正文。
5. `internal_product` 不能读取要求 `internal_training` 的版本；缺失 `required_scope` 拒绝；`customer_public` 同样须匹配 scope。
6. parent 不可见、任一 sibling 不可见、child 清单不完整或父子跨租户/版本时，child 请求不得包含 parent 全文；直接 parent 请求不得绕过检查。
7. 未配置签名服务时，合法请求返回 200、空 URL 和指定状态；响应所有层级不得泄露存储路径、object_key 或 chunk_sha256，包括 locator 内嵌值。
8. 成功与拒绝访问均有相应审计记录；无授权角色不能读取其他租户审计；成功审计落库失败时不返回正文。

【响应字段最终清单】

所有字段均 required：

`evidence_id, knowledge_id, version_id, status, classification, audience, locator, heading_path, text, text_level, text_evidence_id, signed_url, signed_url_state, trace_id`

- 新增 `text_evidence_id`：实际提供正文的 chunk ID；返回 child 时等于 `evidence_id`，恢复 parent 时为 parent ID。
- `locator / heading_path` 对应 **实际文本来源**，须清除私有路径信息，避免 parent 正文被标成 child 定位。
- 允许为 null：`locator`、`heading_path`（源数据缺失时）、`signed_url`。
- 其余字段不可为 null；本切片 `signed_url` 固定 null，`signed_url_state` 固定 `NOT_CONFIGURED`。

【可延后】

签名 URL、有效期与过期不可读验证；引用级独立撤销；历史版本专用取证接口。前者待 ADR-01 与对象存储方案明确后另行送审。

本次仅作裁决，未修改文件；由 L2 将采纳结果登记至 `docs/SPEC/evidence-locator.md`。

【风险提示】

**最容易上线后泄露内容的是“直接请求 parent”绕过全部 child 可见性检查。** 父块恢复规则必须覆盖所有返回 parent 全文的入口。