# 终审送审稿：文档上传与列表接口（实施后）

- 日期：2026-11-07
- 前置：你 2026-11-07 的「有条件通过」裁决（本片按 7 条逐条实施）
- 请只做判断：**不要探索仓库、不要读源码、不要跑测试**。

## 1. 逐条落实

| 裁决条 | 实施 |
|---|---|
| 1 scope | 上传 `knowledge.ingest`、列表 `knowledge.read`、审核沿用 `knowledge.review`；三者互不隐含；`required_scope` 单独做数据范围校验（不在 `allowed_scopes` → 403） |
| 2 传输/体积 | JSON+base64；解码后 ≤5 MiB（恰好 5 MiB 通过、+1 字节 413）；新增唯一错误码 `PAYLOAD_TOO_LARGE`(413)；HTTP 请求体 >8 MiB **在解析前**由中间件按 `Content-Length` 拦；base64 非法/空正文 422；全部在写库前完成 |
| 3 重复 | 同租户 `knowledge_key` 重复 → 409，文案为"已存在"，不覆盖不建版；**由 `UNIQUE(tenant_id, knowledge_key)` 兜底**；同内容重复（`DuplicateContentError`）同样 409；失败不留半成品 |
| 4 列表可见性 | 不做逐块 ACL；`knowledge.read` 定义为文档管理元数据读取；SQL 顺序 = 租户 → `DISTINCT ON(document_id) ORDER BY version_no DESC` 取最新 → `required_scope = ANY(allowed_scopes)` + status/domain 过滤 → `created_at DESC, document_id ASC` 排序分页；最新版不可见则整条消失；上传侧校验：`required_scope` 授权 + 请求 ACL 必须允许调用者本人（否则 403） |
| 5 初始状态 | 请求体无 status 字段（`extra="forbid"`，多传即 422）；入库固定 `DRAFT`；文档说明 review→approve 才可检索 |
| 6 命名 | 新接口返回 `version_id`；旧路径不改，OpenAPI 注释注明 `knowledge_id` 实为 `version_id` |
| 7 目录抽取 | 保持现行为（`product_catalog` 触发抽取与冲突待办），抽取计数走 `warnings` 返回 |

## 2. 测试证据（11 条，全绿；`536 passed` 全量门禁）

1. 上传成功 → `DRAFT`、child_chunks>0、检索 0 命中；审批后可检索（PASS→publish internal→检索命中）
2. 非法 base64 / 空正文 / 非法 domain / classification 越界 → 422
3. 恰好 5 MiB → 201；5 MiB+1 → 413；9 MiB 请求体 → 413（`PAYLOAD_TOO_LARGE`）；超限后库里仍只有 1 个文档
4. 重复键 → 409（文档数 1）；**并发两条同键请求 → 一条 201 一条 409，版本数 1**
5. 只读管理员（仅 `knowledge.read`）能列表、上传 403、审核 403；无 read 的令牌列表 403
6. `required_scope` 越权 → 403；ACL 拒绝上传者本人 → 403
7. 同键第二版放到调用者不可见的 scope → 列表 `total=0`（不回退旧版本）；分页/domain/status 过滤与稳定排序
8. 端到端：上传→审核→发布→检索可命中
9. 目录样例 → `catalog_items > 0`；冲突样例 → 冲突组 ≥1
10. 硬失败原子性：把 `IngestionRepository.insert_chunks` 改成抛错 → 500，事后 文档/版本/切片 均为 0

## 3. 实施中发现的一个既有边界（请确认是否算本片的缺口）

`parse_catalog_text` 对**同一段文本内**重复出现的货号只保留第一条（其余计入 `unsupported`），
而冲突判定是按 **child 块**出现次数与原文差异来算的。因此：

- 「同一页（同一 child 块）里同一货号两行不同价」→ **不会产生冲突待办**，第二行只进 `unsupported` 计数；
- 只有「同一货号跨 child 块出现不同原文」才会进冲突队列。

我按你的要求用「跨页/跨块」样例做了冲突断言（已绿），没有改解析器语义。**这是否需要在后续片处理，请你裁定**：
若需要，改动点在 `fkh.domain.catalog.parse_catalog_text` 的去重分支（把"同块内重复"从 `unsupported` 改为候选），会触及抽取规则与既有目录数据，我倾向单独送审再做。

## 4. 边界与风险

- 未做：删除/回滚/批量上传/对象存储；`catalog_currency` 仍为显式传入（无默认货币）；
- 请求体上限只看 `Content-Length`；chunked 编码请求会进入路由，由解码后 5 MiB 二次校验兜底（已注明）；
- 列表 `offset` 只保证静态数据下稳定，不承诺并发快照一致（已写入 OpenAPI）；
- 旧路径参数命名遗留不改（裁决 6）。

## 5. 请裁决

1. 上述 7 条实施是否满足裁决？若否，指出需要改的点。
2. §3 的解析器边界：本片是否算缺口？是否需要单独立项送审？
3. 是否有必须在对外交付前补的项（例如 chunked 请求的硬上限）。
