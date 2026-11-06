# 工程交接与状态快照

- 快照时间：2026-11-03 20:50
- 对应提交：`b93a61b`（outbox 投递）+ 工作区未提交的 evidence 切片
- 用途：让并行推进的另一条 lane（DSH / 其他开发代理）在不读全部代码的前提下接得上手

## 1. 一句话现状

知识从文件进来到可以被审核、发布、撤销、取证、**按权限检索**的**服务端闭环已经跑通**；
问答生成已接入**本地推理**（ADR-04 未签期只允许本地推理，见 docs/SPEC/answer-generation.md）；
向量检索为 opt-in（`FKH_RETRIEVAL_MODE=hybrid`），生产默认词法。

```
文件 → 解析 → 切片 → 入库(草稿) → 审核 → 内部批准 → 双签公开 → 撤销 → 事件投递 → 受控检索
        ✅     ✅      ✅        ✅      ✅        ✅        ✅      ✅        ✅(词法)
```

## 2. 四条 lane

| Lane | 范围 | 状态 | 关键提交 |
|---|---|---|---|
| A · 采集与治理 | 解析、切片、入库、审核发布、ACL、终止路径 | 已完成（弃用路径 2026-11-05 落地：`DISCARDED` + 迁移 0013） | `16248f2` `6803710` `82eec2e` + 0013 |
| B · 事件投递 | outbox 租约、令牌、死信、重放 | 已完成，告警未接线 | `b93a61b` |
| C · 证据定位 | `GET /v1/evidence/{id}`：判定 + 正文 + 访问审计 | 已完成（`_evidence/l1_reply_evidence_20261103.md`） | `62f0f34`（0007） |
| D · 检索 | 词法 + 向量双路、RRF 融合、ACL 前置、检索审计 | 库层 + HTTP 接线完成：**默认 `lexical`**，`FKH_RETRIEVAL_MODE=hybrid` opt-in；真实 BGE-M3 已跑通（CPU）；词法已改字级 OR（`lexical-v2`）：recall 0.06→0.68；混合 recall 0.84（生产未授权）（`docs/SPEC/recall-benchmark.md`） | `a60755a`（0008）+ `827ba1a` + 0011 接线 |
| 接缝 | 入库 → 审核 → 检索的真实链路 | 已通 | `19e61cf` |
| E · 上下文 | 检索命中 → 生成用上下文 + 引用核验与拒答判定 | 完成（`POST /v1/knowledge/context`） | `50f5a26` |
| H · 词法排序 | 字级 OR 召回 + BM25 重排（`lexical-v3`）：冻结用例 recall 0.48→0.62，+8/-1 | 完成（`_evidence/lexical_v3_benchmark_20261106.md`） | 2026-11-06 |
| G · 目录精确通道 | 货号→规格/单价：入库抽取 + 确定性查询 + 问答路由（不经过模型） | 完成（`docs/SPEC/catalog-exact.md`；客户目录实测 recall 0/30 → 精确查询） | 2026-11-06 |
| F · 问答 | `POST /v1/knowledge/answer`：检索 → 本地推理 → 引用核验 → 状态判定 → 审计 | 完成（**本地推理 only**，`FKH_LLM_PROVIDER=none` 默认 503；ADR-04 未签期唯一允许形态；`docs/SPEC/answer-generation.md`） | 2026-11-06 |
| F · 培训 | 建课、出题、发布、分派、测验判分、进度 | 完成；签核与反馈未做 | 本轮 |

## 3. 怎么跑

```bash
cd H:/MyWork/Factory-Knowledge-Hub
uv sync --all-groups
uv run pytest -q                 # 356 项，本地自动起 pgserver（同一时刻只能跑一份，见 §8.11）
uv run ruff check src tests migrations
uv run mypy
uv run uvicorn fkh.api.main:app --port 8000
```

数据库：本机无 Docker / 无系统 PostgreSQL，测试用 `pgserver` 内嵌 PG 16（自带 pgvector）。
CI 用 `pgvector/pgvector:pg16` service，两条路径跑同一批迁移与断言。

## 4. 代码地图

| 模块 | 职责 | 不能做什么 |
|---|---|---|
| `api/main.py` | 应用装配、契约路由、健康探针 | 不要在业务端点里直接写 SQL |
| `api/security.py` | JWT 校验、主体上下文、scope 收窄 | 不接受请求体覆写 tenant/principal |
| `api/db.py` | 连接池、请求级连接 | 不要在端点内自建连接 |
| `api/routes_review.py` | 审核/发布/撤销端点 | 不要在端点内判断状态机，交给 service |
| `authz/policy.py` | ACL 判定：Deny 优先、缺省拒绝 | 不要加 `*` 通配符 |
| `authz/parent.py` | 父块恢复、片段不宽于父 | 不要只看命中 child 就恢复父块 |
| `domain/state_machine.py` | 7 状态白名单与动作映射 | 改动必须先裁决，见 §7 |
| `domain/review.py` | 审核发布撤销、双签、outbox 写入 | 不要绕过行锁直接 UPDATE 状态 |
| `domain/evidence.py` | 证据定位与受控取文 | 不要暴露存储 key |
| `domain/parsing/` | 五种格式解析、复核标记 | 不要静默降级扫描件 |
| `domain/chunking/` | 父子切片、token 近似 | 不要用字符数代替 token |
| `domain/lexeme.py` | 中文字级分词 | 查询侧必须用同一分词规则 |
| `workers/ingestion.py` | 入库编排 | 不要自动审核 |
| `workers/outbox_relay.py` | 事件领取与投递 | 不要用时间/UUID 排事件顺序 |

`retrieval/`、`gateway/`、`mcp/`、`observability/` 目前是空包，Phase 2 填充。

## 5. 迁移台账

| 编号 | 内容 | 状态 |
|---|---|---|
| 0000 | 扩展 | 已应用 |
| 0001 | 治理核心表 + 不变量触发器 | 已应用 |
| 0002 | pgvector 列与 HNSW 索引 | 已应用（可选） |
| 0003 | 培训增量 | 已应用 |
| 0004 | 全表强制 RLS | 已应用 |
| 0005 | 内容哈希唯一 | 已应用 |
| 0006 | outbox 租约/令牌/死信 | 已应用 |
| 0007 | 证据定位：required_scope + 访问审计 | 已应用（本机） |
| 0008 | 检索审计：retrieval_queries（HMAC 摘要） | 已应用（本机） |
| 0009 | 切片词法分词器版本列（拆开 `tokenizer_id` 与 `lexeme_tokenizer_id`） | 已应用（本机） |
| **0010** | **预留给草稿终止路径（待裁决）** | 占位 |
| 0011 | 检索审计补 `retrieval_mode`（lexical-v1 / hybrid-rrf-v1 / not_executed） | 已应用（本机，回滚/重放已演练） |
| 0012 | 审计枚举补 `lexical-v2`（词法查询 AND→OR） | 已应用（本机） |
| 0013 | 版本终止路径：新增 `DISCARDED` 状态 + `DISCARD` 动作 + `review_actions.from_status` + 培训联动 | 已应用（本机） |
| 0014 | 目录结构化通道：`catalog_items` 表（RLS）+ `rag_queries.route/route_reason` | 已应用（本机） |
| 0015 | 目录冲突人工复核队列：`catalog_conflict_groups` + `catalog_conflict_candidates`（RLS） | 已应用（本机） |
| 0016 | 词法排序 v3：审计枚举补 `lexical-v3`（BM25 字级重排） | 已应用（本机） |
| 0017 | 答案一致性审计：`rag_queries` 加 6 列（规则版本/claim 计数/耗时） | 已应用（本机） |

> 占号冲突处理（2026-11-04）：0009 原登记给草稿终止方案，但接缝修复先用到该号。
> 按「后到者让位」处理，草稿终止顺延到 0010。已应用的迁移不改名——改名会让
> `schema_migrations` 的历史记录与文件名对不上。
> 0011 同理跳过了预留号 0010：**跳号本身允许**——`apply.py` 按文件名顺序应用待执行文件，
> 回滚按**应用顺序**倒序取「实际应用过」的版本（`plan_rollback`），因此缺 0010 不会阻断
> 0011+0009 的两步回滚（实测见 `_evidence/migration_0011_atomicity_20261104.md` §4）。
> 真正禁止的是**倒序回填**：已应用最高版本之后才允许新增迁移，0010 若将来要落地，
> 必须改用新号（`> 0011`），否则 `apply.py` 会显式拒绝（`tests/test_migration_tooling.py`）。

**已应用的迁移文件不得修改**：`migrations/apply.py` 对已应用文件做校验和比对，改动会被判为漂移并拒绝执行。

## 6. 红线

1. **状态机不得擅自扩展**。白名单同时在 `state_machine.py` 与数据库触发器里，7×7 全组合对拍；单方面加一条会立刻失败。
2. **公开发布必须两个不同主体**。三层强制：端点无审批人字段、服务二次比对、数据库 CHECK。
3. **跨租户与不存在都返回 403**，不得出现 404 探测。
4. **撤销立即生效**，不等待索引清理；检索侧必须按状态过滤，不能只依赖向量是否删除。
5. **RLS 测试必须用 `SET ROLE` 降级到无 BYPASSRLS 角色**；用超级用户测 RLS 等于没测。
6. **未签 ADR 的能力必须 fail-closed**：无 OCR 就标 `PARSER_FALLBACK`，无对象存储就返回 `signed_url_state=NOT_CONFIGURED`，不猜默认值。
7. **混合检索只能服务端 opt-in**：`FKH_RETRIEVAL_MODE` 默认 `lexical`，且它不是请求参数；
   显式启用后缺模型/权重/推理失败一律 503，**不得静默降级**成词法结果（判决 Q2）。
8. **`/v1/knowledge/context` 固定词法**：即使 search 已启用混合，context 也不跟随；
   接混合会改变拒答分档，须另行送审（判决 Q7）。
9. **价格/货号类问题走确定性通道**：精确通道**不允许模型参与**；看到货号不等于该接管——
   必须同时是询价/询规格意图；跨版本冲突与原文不一致一律拒答或转人工，不任取一条。
10. **问答只允许本地推理**：ADR-04 未签署前，`/v1/knowledge/answer` 只连 loopback
   （按解析后 IP 校验、不经代理、不跟重定向、不自动拉模型）；接入任何外部 provider 必须先签 ADR-04。
11. **弃用 ≠ 撤销**：`DISCARDED` 只允许从未对外发布的版本进入（RAW/DRAFT/REVIEWED/APPROVED_INTERNAL），
   `PUBLISHED_PUBLIC`/`SUPERSEDED` 必须走 `REVOKED`；两者审计语义不同，不得互换。
12. **词法查询必须保持 OR**：字级 token 用 AND 组合会让自然语言问句几乎零命中（实测 0.06）；
   改这里必须重跑 `docs/SPEC/recall-benchmark.md` 的回归门槛并同步 `scoring` 版本。
13. **生产禁止 `embedding_provider=test`**：伪向量会让召回质量与验收结论失真，
   `build_embedder` 按 `FKH_ENV` 直接拒绝。

## 7. 未裁决事项

| 事项 | 影响 | 文档 |
|---|---|---|
| ~~`DRAFT` / `REVIEWED` 无终止路径~~ | **已裁决（2026-11-05）：方案 B，新增 `DISCARDED`**；见 `_evidence/l1_reply_discarded_status_20261105.md` | `docs/REVIEW-REQUEST-draft-termination.md` |
| 证据定位的 ID 语义与 audience→scope 映射 | 已裁决：chunk id + 新增 `required_scope`（0007） | `docs/SPEC/evidence-locator.md` |
| 检索的可见性口径与审计表 | 已裁决：应用层 ACL + `retrieval_queries`（0008），不做 ACL 下推 | `docs/SPEC/retrieval-lexical.md` |
| ADR-01 对象存储与区域 | 签名链接、备份位置 | 未启动 |
| ADR-02 身份源 | 令牌 claim 结构（含 groups/departments） | 未启动 |
| ADR-04/06 Embedding 模型 | 真实 tokenizer、硬件基线、真实语料 recall | 接线已完成（`_evidence/l1_reply_hybrid_wiring_20261104.md`，0011）：默认词法、混合 opt-in、契约 `scoring` 改 enum；真实 BGE-M3 未装、recall 未测，**生产启用未授权**；问答生成仍阻塞 |
| ADR-08 质量阈值 | RAG Triad 门禁值 | 阻塞验收 |

## 8. 已知坑（踩过的，别再踩）

1. plpgsql 的 `RAISE ... USING ERRCODE` 只接受 5 位 SQLSTATE，写 `'check_violation'` 建函数时直接失败。
2. `SET LOCAL app.tenant_id = %s` 不支持参数绑定，必须 `SELECT set_config(..., true)`。
3. 租户上下文是事务级，commit 后失效；服务方法要在每次调用前重新绑定，不能只在构造函数里设一次。
4. PG 视图默认可绕过基表 RLS，必须 `WITH (security_invoker = true)`。
5. 新版 FastAPI 把 `include_router` 折叠成 `_IncludedRouter`，遍历 `app.routes` 看不到子路由；契约比对要用 `app.openapi()`。
6. 同一事务内 `now()` 恒定，事件顺序不能靠时间排；UUID 排序又与业务顺序无关。
7. `pdfplumber.extract_text_lines()` 返回 `x0/top/x1/bottom`，没有 `bbox` 键。
8. PyMuPDF 是 AGPL-3.0，闭源交付不要用；PDF 解析走 pdfplumber（MIT）。
9. FastAPI 默认的 422 会回显 `input`（即请求体原文）。检索这类可能含敏感 query 的端点
   必须用自定义 `RequestValidationError` 处理器收敛成固定文案，否则明文会进响应、前端日志与 APM。
10. outbox relay 的时间判断一律用 `clock_timestamp()`：`now()` 是**事务开始时间**，
    长连接下会落后于真实时间，表现为「事件到期了却领不到」。
11. **同一时刻只能有一份 `pytest` 在跑**。两条 lane 同时跑会共用同一个 pgserver 实例/端口：
    实测 3 次重叠都表现为「几分钟甚至十几分钟不结束」或大批 `requires_pg` fixture 报错
    （其中一次报 `postgres.exe` 绑定 127.0.0.1 被拒，容易被误判成安全策略拦截；
    同一份代码单独跑是 15 秒 339 项全绿）。**先确认没有别的 pytest 在跑，再起门禁。**
12. 提交一律用**显式路径**（`git add src/... tests/...`），禁止 `git add -A`：
    并行 lane 的工作区里有他人的未提交改动、pgdata 与 pytest 临时文件，
    一次 `-A` 就会把它们带进提交（2026-11-04 曾因此把 42MB 测试库提交进 `827ba1a`）。

## 9. 合并前检查

```bash
uv run pytest -q                                  # 必须全绿且无 skip
uv run pytest -q -m requires_pg                   # 数据库门禁必须真实执行
uv run ruff check src tests migrations
uv run mypy
uv run python migrations/apply.py --url "$URL" --status
```

数据库相关测试出现 skip 视为未验证，不得据此宣布通过。

> **混合检索切片终审（2026-11-04）**：`e75f3c9` + `302c367` 经 L1 判定 **PASS**
> （`_evidence/l1_reply_hybrid_wiring_rereview_20261104.md`）。迁移跳号规则已改成
> 「允许跳号、禁止倒序回填、回滚按应用顺序」，见 WORKING-AGREEMENT §2。
