【判决】

1. **采纳应用层 ACL，保留固定 4 倍候选、上限 200。** 不另写 SQL ACL 求值器。契约明确：`max_results` 是返回上限，不保证填满，也不保证得到所有可见内容中的完整 top-K。ACL 必须在片段生成、响应组装之前完成；版本与策略关联均须约束当前租户，缺失策略拒绝。排序补充 `chunk_id` 作为稳定次序。

2. **采纳不做父块恢复，但须补一道限制。** 禁止把聚合 parent 直接作为普通命中返回，否则即使没有“恢复”操作，也可能通过 parent 的片段泄露无权 child。本轮只返回不承载子块聚合内容的独立块或 child；父块全文继续走证据端点。

3. **采纳统一 `200 + results: []`。** 不对外返回候选数、ACL 拒绝数、“全部被过滤”等标志。该规则统一响应语义，**不保证消除条数与耗时侧信道**。令牌无效、缺少接口权限或请求范围未获授权，仍按鉴权规则拒绝，不能伪装成空结果。

4. **采纳精确匹配，不引入 scope 继承。** `knowledge.search` 仅授予调用接口的权限，不能让调用方通过填写 `knowledge_scope` 自行获得数据范围权限；须另校验主体获准请求该范围。SQL 同时约束：
   - `required_scope` 与请求精确相等，`NULL` 不可见；
   - 状态与 audience 成对匹配：`APPROVED_INTERNAL/internal` 或 `PUBLISHED_PUBLIC/customer_public`。

5. **改法：本轮新增检索审计表，使用 0008。** 不复用 `rag_queries`，不伪造问答状态。成功、空结果、授权拒绝、执行失败均应留痕；成功响应前须完成审计写入，写入失败返回脱敏的服务错误。

6. **改法：不返回原始 `score`，只保证排序。** 保留 `scoring="lexical-v1"` 表明排序版本。`ts_rank_cd` 本身不是隐藏文档数量的统计，不能把删除 score 当作消除存在性推断的措施。

7. **采纳最多 400 个 Unicode 码点，不改按 token。** 本轮没有模型上下文预算，字符限制更适合展示契约。片段只取自已授权命中块，不拼接父块、相邻块；增加 `snippet_truncated`，前端按纯文本渲染。

8. **采纳，且覆盖异常路径。** 原始 query 不得进入日志、APM、异常消息、SQL 参数记录、请求体采集或校验错误响应。可以保留不含请求值、绑定参数及局部变量的堆栈；仅做正常返回值脱敏不够。

另补两项契约约束：查询只匹配 `tokenizer_id='cjk-char-v1'` 的块；**本轮移除顶层 `index_generation`**，材料尚未定义混合代次结果如何对应唯一的 `N`，不能任取一条或取最大值冒充一致快照。

【必须补的测试】

1. 跨租户关联版本或策略、策略缺失、Allow 与 Deny 同时命中、四维均无匹配时，均不得返回该块的任何字段。
2. 仅有 `knowledge.search` 而没有请求范围权限时拒绝；精确范围不符、`required_scope=NULL`、状态与 audience 错配时均不可见，不发生范围继承。
3. parent 含无权 child 时，即使 parent 自身允许访问且词法命中，也不得返回 parent 片段；授权 child 可以独立返回。
4. 大量高排名候选被 ACL 拒绝时，允许不足额，但返回不超过上限；无匹配与全部拒绝均为相同空结果结构，且无候选数或过滤原因。同分排序稳定。
5. 中文及含 `& ! ( )` 的查询始终经统一分词和 `plainto_tsquery`；不发生语法注入。分词后无词项时返回空结果，错误 tokenizer 的块不命中。
6. 长文本、补充平面字符的片段不超过 400 码点且不破坏字符；截断标志正确；响应不得出现 `score`、全文或未授权块的定位信息。
7. 成功、空结果、授权拒绝及执行异常均形成相应审计记录；审计写入失败时不得返回成功检索结果。
8. 使用唯一敏感 query 触发正常、校验失败和数据库异常路径，该明文均不得出现在日志、追踪及错误响应中；审计仅出现预期摘要。

【响应字段最终清单】

顶层：

- `results`
- `scoring`：固定 `"lexical-v1"`
- `trace_id`

每条结果：

- `chunk_id`
- `version_id`
- `knowledge_id`：明确映射 `knowledge_versions.document_id`
- `text_snippet`
- `snippet_truncated`
- `locator`
- `heading_path`

定位信息均来自同一已授权命中块。不返回 `score`、`index_generation`、总命中数或过滤统计。

【是否新增审计表】

**是：`retrieval_queries`，迁移 0008。**

列清单：

`id, tenant_id, principal_id, query_hash, query_hash_key_id, knowledge_scope, status, refusal_code, returned_count, trace_id, duration_ms, created_at`

约束：

- `query_hash` 使用服务端密钥的 HMAC，避免短查询被普通哈希字典反推；密钥不入表。
- `status` 使用检索自己的 `SUCCEEDED / REFUSED / ERROR`。
- 无法确定返回数时 `returned_count=NULL`；正常空结果为 `0`。
- 不存 query 明文、片段、候选数或“全部被过滤”标志；审计查询权限单独限制。

【可延后】

ACL 下推优化、完整可见 top-K、父块恢复、范围继承、代次快照协议，以及向量检索和生成。分词改进可延后，当前字级 AND 的语义须写入规格。

【风险提示】

**最容易上线后咬人的是候选挤占。** 大量无权内容可能把有权内容挤出前 200，使可见结果消失；这既造成漏检，也保留存在性推断线索。统一空结果和隐藏 score 都不能解决它，规格不得承诺“空结果说明没有可见相关内容”或“完全隐藏无权内容的存在”。