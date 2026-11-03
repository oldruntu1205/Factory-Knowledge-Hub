# 工程交接与状态快照

- 快照时间：2026-11-03 20:50
- 对应提交：`b93a61b`（outbox 投递）+ 工作区未提交的 evidence 切片
- 用途：让并行推进的另一条 lane（DSH / 其他开发代理）在不读全部代码的前提下接得上手

## 1. 一句话现状

知识从文件进来到可以被审核、发布、撤销、取证的**服务端闭环已经跑通**；检索与问答（Phase 2）尚未开始。

```
文件 → 解析 → 切片 → 入库(草稿) → 审核 → 内部批准 → 双签公开 → 撤销 → 事件投递
        ✅     ✅      ✅        ✅      ✅        ✅        ✅      ✅(已实现)
```

## 2. 三条 lane

| Lane | 范围 | 状态 | 关键提交 |
|---|---|---|---|
| A · 采集与治理 | 解析、切片、入库、审核发布、ACL | 已完成，等待草稿终止裁决 | `16248f2` `6803710` `82eec2e` |
| B · 事件投递 | outbox 租约、令牌、死信、重放 | 已完成，告警未接线 | `b93a61b` |
| C · 证据定位 | `GET /v1/evidence/{id}`：判定 + 正文 + 访问审计 | 已完成（`_evidence/l1_reply_evidence_20261103.md`） | 工作区 evidence 切片（0007） |

## 3. 怎么跑

```bash
cd H:/MyWork/Factory-Knowledge-Hub
uv sync --all-groups
uv run pytest -q                 # 257 项，本地自动起 pgserver
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
| **0008** | **预留给草稿终止路径（待裁决）** | 占位 |

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
| ADR-01 对象存储与区域 | 签名链接、备份位置 | 未启动 |
| ADR-02 身份源 | 令牌 claim 结构（含 groups/departments） | 未启动 |
| ADR-04/06 Embedding 模型 | 真实 tokenizer、向量维度 | 阻塞检索 |
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

## 9. 合并前检查

```bash
uv run pytest -q                                  # 必须全绿且无 skip
uv run pytest -q -m requires_pg                   # 数据库门禁必须真实执行
uv run ruff check src tests migrations
uv run mypy
uv run python migrations/apply.py --url "$URL" --status
```

数据库相关测试出现 skip 视为未验证，不得据此宣布通过。
