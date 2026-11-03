# 工程交接与状态快照

- 快照时间：2026-11-03 20:50
- 对应提交：`b93a61b`（outbox 投递）+ 工作区未提交的 evidence 切片
- 用途：让并行推进的另一条 lane（DSH / 其他开发代理）在不读全部代码的前提下接得上手

## 1. 一句话现状

知识从文件进来到可以被审核、发布、撤销、取证、**按权限检索**的**服务端闭环已经跑通**；
问答生成（Phase 2 后半）与向量检索尚未开始。

```
文件 → 解析 → 切片 → 入库(草稿) → 审核 → 内部批准 → 双签公开 → 撤销 → 事件投递 → 受控检索
        ✅     ✅      ✅        ✅      ✅        ✅        ✅      ✅        ✅(词法)
```

## 2. 四条 lane

| Lane | 范围 | 状态 | 关键提交 |
|---|---|---|---|
| A · 采集与治理 | 解析、切片、入库、审核发布、ACL | 已完成，等待草稿终止裁决 | `16248f2` `6803710` `82eec2e` |
| B · 事件投递 | outbox 租约、令牌、死信、重放 | 已完成，告警未接线 | `b93a61b` |
| C · 证据定位 | `GET /v1/evidence/{id}`：判定 + 正文 + 访问审计 | 已完成（`_evidence/l1_reply_evidence_20261103.md`） | `62f0f34`（0007） |
| D · 检索 | `POST /v1/knowledge/search`：词法召回 + ACL 前置 + 检索审计 | 词法部分完成，向量/问答未开始 | 62f0f34 之后 `a60755a`（0008） |
| 接缝 | 入库 → 审核 → 检索的真实链路 | 已通（本轮修复） | 工作区 `test_ingest_to_search` |

## 3. 怎么跑

```bash
cd H:/MyWork/Factory-Knowledge-Hub
uv sync --all-groups
uv run pytest -q                 # 274 项，本地自动起 pgserver
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

> 占号冲突处理（2026-11-04）：0009 原登记给草稿终止方案，但接缝修复先用到该号。
> 按「后到者让位」处理，草稿终止顺延到 0010。已应用的迁移不改名——改名会让
> `schema_migrations` 的历史记录与文件名对不上。

**已应用的迁移文件不得修改**：`migrations/apply.py` 对已应用文件做校验和比对，改动会被判为漂移并拒绝执行。

## 6. 红线

1. **状态机不得擅自扩展**。白名单同时在 `state_machine.py` 与数据库触发器里，7×7 全组合对拍；单方面加一条会立刻失败。
2. **公开发布必须两个不同主体**。三层强制：端点无审批人字段、服务二次比对、数据库 CHECK。
3. **跨租户与不存在都返回 403**，不得出现 404 探测。
4. **撤销立即生效**，不等待索引清理；检索侧必须按状态过滤，不能只依赖向量是否删除。
5. **RLS 测试必须用 `SET ROLE` 降级到无 BYPASSRLS 角色**；用超级用户测 RLS 等于没测。
6. **未签 ADR 的能力必须 fail-closed**：无 OCR 就标 `PARSER_FALLBACK`，无对象存储就返回 `signed_url_state=NOT_CONFIGURED`，不猜默认值。

## 7. 未裁决事项

| 事项 | 影响 | 文档 |
|---|---|---|
| `DRAFT` / `REVIEWED` 无终止路径 | 错误草稿永久滞留、版本号被占 | `docs/REVIEW-REQUEST-draft-termination.md` |
| 证据定位的 ID 语义与 audience→scope 映射 | 已裁决：chunk id + 新增 `required_scope`（0007） | `docs/SPEC/evidence-locator.md` |
| 检索的可见性口径与审计表 | 已裁决：应用层 ACL + `retrieval_queries`（0008），不做 ACL 下推 | `docs/SPEC/retrieval-lexical.md` |
| ADR-01 对象存储与区域 | 签名链接、备份位置 | 未启动 |
| ADR-02 身份源 | 令牌 claim 结构（含 groups/departments） | 未启动 |
| ADR-04/06 Embedding 模型 | 真实 tokenizer、向量维度 | 阻塞向量检索与问答生成 |
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

## 9. 合并前检查

```bash
uv run pytest -q                                  # 必须全绿且无 skip
uv run pytest -q -m requires_pg                   # 数据库门禁必须真实执行
uv run ruff check src tests migrations
uv run mypy
uv run python migrations/apply.py --url "$URL" --status
```

数据库相关测试出现 skip 视为未验证，不得据此宣布通过。
