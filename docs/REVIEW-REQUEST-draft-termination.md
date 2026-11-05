# 审查请求：DRAFT 状态的终止路径缺失

> **状态（2026-11-05）：已裁决。** 采纳**方案 B**：新增 `DISCARDED`（从未公开发布、主动作废），
> 允许 `RAW / DRAFT / REVIEWED / APPROVED_INTERNAL` 进入；`PUBLISHED_PUBLIC / SUPERSEDED` 必须走撤销；
> 迁移 `0013`，事件 `knowledge.version.discarded.v1`，培训课程同步转 `NEEDS_REVIEW`。
> 回执：`_evidence/l1_reply_discarded_status_20261105.md`；实现见 `tests/e2e/test_discard_flow.py`。
> 下文为原始请求，保留作决策记录。

- **提出日期**：2026-11-03
- **提出方**：L2 实施
- **需裁决方**：L1 架构 / 业务负责人
- **阻塞程度**：不阻塞 Phase 1 演示；阻塞 Phase 1 上线（草稿会持续堆积）
- **相关条款**：L1 §3.2 状态机、§7.2 撤销硬规则、§5.2 CHECK 约束

## 1. 问题

L1 §3.2 定义的状态迁移是：

```text
RAW -> DRAFT -> REVIEWED -> APPROVED_INTERNAL -> PUBLISHED_PUBLIC
                  ↑ 退回 DRAFT
批准版本可转 SUPERSEDED 或 REVOKED
```

实现时发现：**DRAFT 没有出口**。除了 `DRAFT -> REVIEWED`（提交审核），没有任何一条
合法路径能让一个草稿离开系统。REVIEWED 之后可以退回 DRAFT，但退回的 DRAFT 同样出不去。

同一缺口还有第二种表现：**`REVIEWED -> REVOKED` 也不在白名单里**。
审核已通过、尚未批准的内容如果被发现有误，既不能直接撤销，退回 DRAFT 之后也卡死。

具体后果：

1. 上传错文件（拿错型号、传了别的客户资料）后，这份草稿会永久留在库里；
2. 版本号被占用，`version_no` 持续递增，后续版本号与实物文档对不上；
3. 该版本的所有切片仍在 `knowledge_chunks` 里，只是状态不是 APPROVED，检索过滤能挡住，
   但后台列表、统计、存储开销都还在；
4. 审核人队列里会堆积永远不会处理的草稿。

## 1.1 实测复现

```text
tests/e2e/test_review_flow.py::test_draft_cannot_be_revoked_pending_adr   # DRAFT -> REVOKED 被拒
tests/e2e/test_review_api.py::test_revoke                                 # 需先 APPROVE_INTERNAL 才能撤销
```


## 2. 现状实现

按"冻结契约不擅自扩展"的原则，当前实现**不允许** `DRAFT -> REVOKED`：

```
tests/e2e/test_review_flow.py::test_draft_cannot_be_revoked_pending_adr
```

撤销只对 `REVIEWED` / `APPROVED_INTERNAL` / `PUBLISHED_PUBLIC` / `SUPERSEDED` 开放。
这是保守取值：宁可让草稿留着，也不要在没有裁决的情况下扩展状态机。

## 3. 候选方案

### 方案 A：允许 DRAFT → REVOKED

- 改动最小，只加一条迁移，不需要新状态、新事件、新 UI。
- 代价：REVOKED 的语义变成"作废"，混合了两种完全不同的情况——
  "曾经发布、后来必须撤回"和"从未发布、只是写错了"。
  审计上无法回答"这份内容是否曾经对外可见"。

### 方案 B：新增 DISCARDED 状态（推荐）

- 语义清晰：`DISCARDED` = 从未发布、主动作废；`REVOKED` = 曾经发布、事后撤回。
- 需要：状态枚举 + CHECK 约束 + 迁移白名单 + 新事件 `knowledge.version.discarded.v1`
  + 检索过滤 + 培训课程联动（引用被弃用版本的课程是否也要 NEEDS_REVIEW，见问题 2）。
- 代价：改动面比 A 大，但审计语义唯一。

### 方案 C：软删除标记（`discarded_at`）

- 不动状态机，加一列时间戳，所有查询追加 `discarded_at IS NULL`。
- 代价：状态与"是否作废"变成两个维度，容易在某个查询里漏掉条件。
  已有的 RLS 与状态过滤已经够复杂，再加一个隐式维度风险偏高。

## 4. 建议

选 **方案 B**。理由是审计：

> 客户问"这份资料你们什么时候对外公开过？"——方案 A 无法回答，
> 因为 REVOKED 里混着从未发布过的草稿。

数据量与存储成本不构成理由：草稿切片本来就该保留一段时间以供追溯。

## 5. 需要裁决的具体问题

1. 采用 A / B / C 哪一个？
2. 若选 B：`DISCARDED` 是否允许从 `RAW` 直接进入（上传即发现错误）？
3. 若选 B：引用被弃用版本的培训课程是否同样转 `NEEDS_REVIEW`（当前只有 REVOKED 触发）？
4. 草稿与弃用版本的保留期限？（涉及 ADR-05 的保留策略，可能同一套规则）
5. 版本号是否需要回收？（建议不回收：版本号一旦分配就不可复用，否则引用会串）

## 6. 在裁决前的临时约定

- 生产环境不得让草稿长期堆积。运营侧的处理方式是：上传错误时用**新版本**覆盖正确内容，
  旧草稿留在 DRAFT 状态并在后台列表标注，不对外可见。
- 该约定写进 `docs/runbooks/`，上线前必须由业务负责人确认可接受。

---

## 附：可直接投喂的审查提示词

> 以下内容可整段复制给外部审查方（ChatGPT / 架构评审人），无需额外上下文。

```text
你是制造企业知识管理系统的架构评审人。请审查一个状态机缺口，并给出裁决建议。

【背景】
系统是一个企业知识中枢，知识版本有 7 个状态：
RAW → DRAFT → REVIEWED → APPROVED_INTERNAL → PUBLISHED_PUBLIC
以及 SUPERSEDED、REVOKED。
业务约束：
- 知识分 C0（可公开）到 C4（核心机密）五级，只有 C0 且 audience=customer_public 才能对外发布；
- 对外发布必须两个不同主体分别批准（技术审批 + 公开审批），不允许同一人双签；
- 版本内容不可变，修改产生新版本；
- 撤销（REVOKED）要求立即生效，不等索引异步清理。

【当前允许的状态迁移】
RAW → DRAFT
DRAFT → REVIEWED
REVIEWED → APPROVED_INTERNAL
REVIEWED → DRAFT（退回）
APPROVED_INTERNAL → DRAFT（退回）
APPROVED_INTERNAL → PUBLISHED_PUBLIC
APPROVED_INTERNAL → SUPERSEDED
APPROVED_INTERNAL → REVOKED
PUBLISHED_PUBLIC → SUPERSEDED
PUBLISHED_PUBLIC → REVOKED
SUPERSEDED → REVOKED

【问题】
DRAFT 与 REVIEWED 都没有通往 REVOKED 或任何终止状态的路径。
后果：
1. 上传错文件（拿错型号、传了其他客户的资料）后，这份草稿永久留在库里；
2. version_no 被占用并持续递增，后续版本号与实物文档对不上；
3. 切片仍在索引表中，只是状态不是 APPROVED，靠检索过滤挡住，但存储与统计开销仍在；
4. 审核队列堆积永远不会处理的草稿。

【三个候选方案】
A. 允许 DRAFT → REVOKED，复用撤销语义。
   改动最小，但 REVOKED 会同时表示"曾发布后被撤回"和"从未发布只是写错"，
   审计上无法回答"这份内容是否曾经对外可见"。
B. 新增 DISCARDED 状态，语义为"从未发布、主动作废"，与 REVOKED 区分。
   需要新状态、新事件、检索过滤、培训课程联动，改动面最大。
C. 加软删除列 discarded_at，不动状态机，所有查询追加条件。
   状态与"是否作废"变成两个维度，容易在某个查询里漏掉条件。

【请输出】
1. 推荐方案及理由，明确指出你否决其他方案的关键原因；
2. 若推荐 B，请说明 DISCARDED 是否也应允许从 RAW 直接进入；
3. 被作废版本引用的下游对象（培训课程）应如何处理；
4. 版本号是否应该回收，为什么；
5. 这个改动对"撤销必须立即生效"这一约束是否有影响。
请直接给结论，不要复述问题。
```

---

## 附：迁移编号占位（2026-11-03 补记，实施侧）

`0006_outbox_delivery.sql`（outbox 投递）、`0007_evidence_access.sql`（证据定位）、
`0008_retrieval_queries.sql`（检索审计）已被占用。本问题的裁决若选方案 B/C，请使用 **`0009`**，
避免与已应用迁移撞号；上文的问题清单不变。
