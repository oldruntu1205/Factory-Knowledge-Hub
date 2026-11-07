# 设计稿：文档上传与列表接口（业务前端「文档审核页」前置）

- 日期：2026-11-07
- 提出方：L2 实施
- 风险归类命中：**对外 API 契约新增**、**权限模型扩展**、**写入不可逆数据（入库）**
- 请只做判断：**不要探索仓库、不要读源码、不要跑测试**。

## 1. 背景

业务前端要能「传文档 → 审核 → 发布」，但**目前没有任何 HTTP 入库入口**：
入库只在 service 层（`ingest_document`）与内部工具里调用；审核/发布/撤销已有 4 个端点。
所以这一片必须新增两个端点，否则审核页没有东西可审。

现状（已实现、已被裁决过的语义）：

- 入库走 `ingest_document`：切片（parent/child）、写 ACL、`product_catalog` 域自动抽目录条目；
- 审核流：`POST /v1/knowledge/{id}/review`（PASS/REJECT/APPROVE）、`/publish`、`/revoke`、`/discard`；
  这些路径里的 `{knowledge_id}` **实际是 version_id**（历史命名），前端要按 version 传；
- 所有写入都在租户 RLS 内，身份取自令牌。

## 2. 拟议接口

### 2.1 `POST /v1/knowledge/documents`（上传并入库）

```json
{
  "filename": "catalog_2023.md",
  "title": "三爱思 2023 产品目录",
  "domain": "product_catalog",
  "owner_department": "sales",
  "classification": 2,
  "audience": "internal",
  "required_scope": "internal_product",
  "acl_allow_roles": ["staff"],
  "acl_deny_roles": [],
  "knowledge_key": "catalog-2023",
  "content_base64": "IyDnm67lvZU…",
  "catalog_currency": "CNY"
}
```

- 传输：**JSON + base64**（不用 multipart：`python-multipart` 虽已安装，但 base64 不需要额外依赖与边界处理，错误也更可控）；
- 解码后大小上限 **5 MiB**，超出 413（新增错误码 `PAYLOAD_TOO_LARGE`，或你指定复用 422）；
- `knowledge_key` 唯一：同租户重复 → 409 `VERSION_CONFLICT`；
- 返回：`{document_id, version_id, status, chunks, catalog_items, warnings}`；
- 入库后版本处于初始状态（现为 `DRAFT`），需走 review 才可被检索。

### 2.2 `GET /v1/knowledge/documents`（审核列表）

- 参数：`status`（可选，`DRAFT|REVIEWED|APPROVED_INTERNAL|PUBLISHED_PUBLIC|REVOKED|DISCARDED|SUPERSEDED`）、
  `domain`、`limit`（1–200，默认 50）、`offset`；
- 返回每个文档的最新版本：`{document_id, knowledge_key, title, domain, owner_department,
  version_id, version_no, status, classification, audience, required_scope, chunk_count, updated_at}`；
- 排序：`updated_at DESC, document_id ASC`（补唯一键）。

## 3. 待判问题（请逐条）

1. **scope 命名**：上传用 `knowledge.ingest`（新）、列表与审核用 `knowledge.review`；还是列表用 `knowledge.read`（新）？我倾向：
   上传 `knowledge.ingest`、列表 `knowledge.read`、审核沿用 `knowledge.review` —— 让"只读管理员"能看不能批。
2. **传输与体积**：base64 + 5 MiB 上限 + 413 新错误码，是否可接受？若你要求 multipart，请说明边界处理口径。
3. **幂等**：`knowledge_key` 重复 → 409（不覆盖、不静默新建版本），是否可接受？
4. **列表可见性**：文档本身没有 ACL（ACL 挂在 chunk 上），我拟按「租户 + 该文档最新版本的 `required_scope` 在令牌授权内」过滤；
   不做逐块 ACL（审核人需要看到全貌）。这和冲突接口「任一候选被拒即整组隐藏」不同，请确认这个差异可接受。
5. **初始状态**：入库即 `DRAFT`（不可被检索），必须 review→approve 才可用；是否同意不让上传者直接置为可检索？
6. **命名遗留**：review/publish/revoke 路径参数叫 `knowledge_id` 但实际是 `version_id`。本片**不改**旧路径（契约稳定性），
   只在新接口里用 `version_id` 命名，并在文档注明；你是否要求顺带纠正？
7. **上传是否触发目录抽取**：`product_catalog` 域自动抽条目并可能产生冲突待办（现行为）。是否保持？

## 4. 实施侧要补的断言（供裁剪）

1. 上传成功 → 返回 version_id，状态 `DRAFT`，chunks>0；
2. 超限 → 413（或你指定码），且**不写库**；
3. `content_base64` 非法 / `domain` 非法 / `classification` 越界 → 422；
4. `knowledge_key` 重复 → 409，且不新增文档/版本；
5. 缺 scope → 403；跨租户看不到别人的文档；
6. 列表：按 status/domain 过滤、分页边界、排序稳定（同 updated_at 时按 document_id）；
7. 列表只返回**最新版本**（同文档多版本时）；
8. 上传后走 review→approve，检索能命中（端到端）；
9. 上传 `product_catalog` 文本 → 触发目录抽取（条数 > 0）；
10. OpenAPI 与实现一致；错误体符合 `error.json`。

## 5. 边界

- 本片不做：删除文档、版本回滚、批量上传、对象存储（内容进库，私域小体量）；
- 不新增表；新增错误码最多 1 个（`PAYLOAD_TOO_LARGE`）；
- 一次调用回完 §3 的 1–7 + 对 §4 的裁剪；实现后终审另计 1 次。
