# 入库链路规格

对应 L1 §3.2、§3.3、§4.2 与 §5。覆盖"文件进来，到一份可审核草稿"这一段。

## 1. 流程

```text
request
  → parse_document          解析为块序列（带页码/坐标/标题）
  → build_chunks            切父子块（token 近似 + 标题分组）
  → [事务开始]
      ensure_document       按 knowledge_key 取或建 knowledge_documents
      find_version_by_content 同内容哈希已存在则拒绝
      create_acl            acl_policies 写入策略 JSON
      create_version        以 RAW 建版本
      next_index_generation 分配索引代号（租户内 advisory lock）
      insert_chunks         先父块后子块，写 lexeme
      transition RAW→DRAFT  同时写 review_actions(SUBMIT)
  → [提交由调用方决定]
```

## 2. 为什么只到 DRAFT

入库不自动审核。L1 §3.3 要求语音访谈与资料都要人工过一遍再进索引；即便解析器很确定，
"内容对不对"也不是程序能判断的。所以 `ingest_document` 的产出永远是草稿，
`APPROVED_INTERNAL` 及之后的状态必须由 `knowledge.review` 权限的独立主体推进。

## 3. 事务与 RLS

- 整个入库在一个事务内完成，内部用 savepoint 保护。失败时不留半成品版本——
  半成品比失败更危险，它会被后续查询当作真实知识。
- 连接必须携带 `app.tenant_id`。未设置时 RLS 让所有读写返回零行，
  表现为"写入成功但查不到数据"，因此 `IngestionRepository.__init__` 强制设置一次。
- 业务角色需无 `BYPASSRLS`、非表 owner，否则 RLS 不生效（测试用 `SET ROLE` 降级验证）。

## 4. 重复内容

两道防线：

1. 应用层 `find_version_by_content`：给出可读错误并返回既有版本号；
2. 数据库 `uq_versions_document_content (tenant_id, document_id, content_sha256)`：
   并发下两个请求可能同时通过应用层检查，唯一性只能靠约束。

应用层检查的价值在于错误信息，不在于正确性。

## 5. 中文分词（lexeme）

`lexeme` 由 `fkh.domain.lexeme.segment_cjk` 写入，当前是**字级**方案：
CJK 字符之间插空格，交给 `to_tsvector('simple', ...)` 建字级倒排。

实测（40KB 中文契约文档，17 parent / 32 child）：

| 查询 | 命中切片 | 说明 |
|---|---|---|
| 冷凝管 | 4 | 正常命中 |
| 新人培训 | 14 | 正常命中 |
| 离心泵 | 0 | 文档中不存在，正确不命中 |
| 撤权 | 20 | **过度命中**：「撤」与「权」分别命中「撤销」「权限」 |

最后一行是这个方案的已知代价：字级 AND 不保证词序与词边界。查询侧统一用
`plainto_tsquery('simple', segment_cjk(q))`，不手工拼 tsquery（`&` `!` `(` 会破坏语法）。

Phase 2 引入真实分词器时，必须同时重建 lexeme 与查询侧规则——两侧分词不一致会永久失配。

## 6. 未做与原因

| 未做 | 原因 |
|---|---|
| 生成 Embedding | 模型待 ADR-04/06；写错模型的向量无法追溯，宁可留空 |
| 写入对象存储 | 对象存储位置待 ADR-01；当前只记 `source_object_key` 占位 |
| 自动审核 / 自动发布 | 违反 L1 §3.2 双审批与 §3.3 人工环节 |
| OCR | 引擎待 ADR-05；扫描件的页面已标记 `PARSER_FALLBACK` |
| outbox 事件 | 目前只有入库一个生产者，事件化留到审核与索引发布链路一起做 |

## 7. 验收证据

- `tests/e2e/test_ingestion_flow.py`（10 项）：版本与审计记录、父子引用完整性、
  标题路径与定位、中文检索命中与不命中、重复内容拒绝、版本号递增、跨租户不可见、
  复核原因落审计、数据库唯一约束。
- `tests/security/test_state_machine_parity.py`（46 项）：应用层与数据库状态机
  在 42 个非平凡组合上逐一比对。
