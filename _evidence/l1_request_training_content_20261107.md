# L1 送审：培训课程正文本读取接口（新契约）

- 日期：2026-11-07
- 风险归类：**新增对外契约**、**权限与可见性**、可能涉及**分级边界**
- 请只做判断：**不要探索仓库、不要读源码、不要跑测试**。

## 1. 为什么需要

Rev2 §1.1-6 / §13：学员必须"阅读批准的培训内容"。现在学员只有 `GET /v1/training/me/assignments`
（任务元数据）与 `GET /v1/training/assignments/{id}/progress`（进度），**没有任何读取正文的入口**，
链路在"阅读"这一步断开（问答 `POST /v1/training/ask` 已接通，但没有原文可看）。

## 2. 拟议接口

```
GET /v1/training/assignments/{assignment_id}/content?cursor=<opaque>&limit=<1..20>
```

- **权限**：`learner` scope；任务必须属于令牌主体（他人任务与不存在任务同码 403）；
  课程必须 `PUBLISHED`（撤销联动后为 `NEEDS_REVIEW` → 409）。
- **返回**：按课程源（`training_course_sources`）顺序，逐块返回**父块（parent chunk）**：
  `{version_id, title, version_status, blocks: [{chunk_id, heading_path, locator, text, token_count}], next_cursor}`
- **只返回**：`APPROVED_INTERNAL`（课程源在发布时已校验）且 `required_scope` 被令牌
  `allowed_scopes` 包含、ACL 允许的块；**不返回** `DRAFT`/`REVOKED`/`DISCARDED`/`SUPERSEDED` 版本。
- **分页**：cursor 为不透明串（建议 base64(version_ordinal, block_ordinal)），`limit` 默认 5、上限 20；
  不接受"跳过检查直接给全文"的参数。
- **不返回**：object_key、存储路径、chunk_sha256、claimed 之外的任何内部标识。

## 3. 需要你判断的五点

1. **粒度**：返回父块列表（带 heading/locator）而非整篇拼接文本——你认可吗？
   我倾向父块：可与问答引用对齐、可定位、可分页；整篇拼接既大又丢结构。
2. **scope 复用**：沿用 `learner`，还是新增 `training.read`？我倾向**沿用 `learner`**
   （学员身份本身就是能力边界），避免 scope 泛滥。
3. **可见性口径**：培训内容是否也只认 `internal_training` + `APPROVED_INTERNAL`？
   我拟直接复用对客 P0 收敛后的**同一谓词**（`is_external_customer` + status/audience 配对），
   即学员按内部身份读 `APPROVED_INTERNAL`，并且必须是课程源、必须是其 `allowed_scopes` 覆盖的范围。
   这意味着：**课程源之外的同域内容一律不返回**（防止"拿一个任务读全库培训材料"）。
4. **分级**：是否需要按 `classification` 做额外限制（例如 C3 培训只给白名单岗位）？
   我倾向本片**不加**（课程已由 `training_admin` 选源 + `training_publisher` 发布），
   分级限制留到 ADR-03 明确后再做；若你认为必须现在就按分级过滤，请给口径。
5. **审计**：是否要求每次正文读取都落审计（谁、哪个课程、哪些块）？
   我倾向**落一条轻量审计**（`training_content_access_log`），但那是**新表 + 迁移**；
   若你希望复用现有审计表或干脆本片不落，请指示。

## 4. 实施侧拟补的断言

1. 学员读自己的任务 → 200，块来自课程源；块顺序稳定；
2. 学员读他人任务 → 403（与不存在同码）；
3. 未发布/撤销后课程 → 409；
4. 版本撤销后 → 该版本块不再出现（且整体不可读）；
5. 课程源之外的同域版本 → 不出现在结果里；
6. 分页：`limit` 边界、`next_cursor` 可续读、读到末尾时 `next_cursor=null`；
7. 缺 `internal_training` 数据范围 → 403；
8. 不返回 object_key / 存储路径 / sha256（字段白名单断言）。

## 5. 边界

- 本片不做：正文搜索、批注、阅读进度打点（另议）、前端美工；
- 若你要求审计表，会追加一个迁移（`0020`），届时同批送审。
