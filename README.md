# Factory Knowledge Hub V1.0

制造业 AI 企业知识中枢。把工厂的产品资料、老板与老师傅的经验、外贸客服口径、新人培训内容，收敛为**可审核、可检索、可引用、可撤销**的知识资产。

- 业务侧说明：[`Factory-Knowledge-Hub-V1.0-客户展示与ROI研究报告.html`](./Factory-Knowledge-Hub-V1.0-客户展示与ROI研究报告.html)
- 技术与验收基线：[`Factory-Knowledge-Hub-V1.0-L1-架构与开发契约-Rev2-含新人培训.md`](./Factory-Knowledge-Hub-V1.0-L1-架构与开发契约-Rev2-含新人培训.md)
- 交接与当前状态：[`docs/HANDOFF.md`](./docs/HANDOFF.md)
- 并行开发约定：[`docs/WORKING-AGREEMENT.md`](./docs/WORKING-AGREEMENT.md)

本仓库当前处于 **Phase 0（契约与基础设施）**。Phase 0 的完成条件是门禁测试通过，而不是代码量。

## 1. 已冻结的业务决策

| 编号 | 决策 |
|---|---|
| D-01 | 混合部署；敏感知识在私有域处理，外部模型调用必须经企业 AI Gateway |
| D-02 | 经验采集优先语音访谈；原始录音不可直接对外检索 |
| D-03 | 系统独立部署，未来以 API/MCP 接入 AI Factory |
| D-04 | 首批试点：产品知识 / 外贸 AI 客服 / 新人培训 |
| D-05 | 对外业务保持人工审核，不自动报价、不承诺交期、不自动发信 |
| D-06 | 新人培训正式纳入 V1.0 首批上线范围 |

未签署的 ADR 见 [`docs/ADR/`](./docs/ADR/)。**未经签署的 ADR 不得被代码默认为已批准**：涉及部署区域、身份源、数据分级、模型路由、录音授权、SLO、客服接入、质量阈值的取值，一律以配置项 + 显式未配置报错处理。

## 2. 目录结构

L1 第 10 章给出的仓库结构是目标形态。Phase 0 采用**单包多模块**落地，避免在服务边界尚未稳定时拆分成多个可独立构建的包。

```text
.
├── src/fkh/                 # 唯一 Python 包（apps/* 与 packages/* 的合并实现）
│   ├── api/                 # knowledge-api：路由、依赖、错误映射
│   ├── authz/               # OIDC 校验、ACL 判定、父块恢复
│   ├── domain/              # 领域逻辑（状态机、审核发布、分词）
│   │   ├── parsing/         # PDF/DOCX/HTML/Markdown/CSV 解析与复核标记
│   │   └── chunking/        # 父子切片、token 近似计数
│   ├── retrieval/           # Dense/Lexical、RRF、rerank（Phase 2）
│   ├── gateway/             # LLM Gateway 分级路由与脱敏（Phase 2）
│   ├── observability/       # trace、审计、指标
│   ├── workers/             # ingestion 入库编排与仓储（interview/evaluation 待建）
│   └── mcp/                 # 受控 MCP 适配（只读工具）
├── contracts/               # OpenAPI 3.1、JSON Schema、事件 Schema（契约单一来源）
├── migrations/              # 可重放 SQL 迁移 + 回滚脚本
├── tests/                   # contract / security / retrieval / evaluation / e2e
├── deploy/                  # compose / nginx / backup
└── docs/                    # ADR / SPEC / runbooks / acceptance
```

拆分时机：当某个服务需要独立伸缩或独立发布镜像时，再把 `src/fkh/<模块>` 提升为独立包。决策记录见 `docs/ADR/ADR-000-工程布局.md`。

## 3. 本机环境现状（2026-11-03 实测）

| 组件 | 状态 | 影响 |
|---|---|---|
| Python | 3.13.14 系统版；项目经 uv 固定为 **3.12.13** | 符合 L1 要求 |
| uv | 0.12.5 | 依赖与解释器来源 |
| Git | 2.55.0 | 已初始化仓库 |
| Docker | **未安装** | 无法本地起 compose 编排 |
| PostgreSQL / psql | **未安装系统服务** | 改用 pgserver 内嵌实例 |
| pgserver（内嵌 PG 16.2 + pgvector） | **已安装，可运行** | 迁移与 RLS 门禁可本地实跑 |
| WSL | **未安装** | 无 Linux 兜底环境 |

数据库门禁不再依赖 Docker：`tests/conftest.py` 优先读 `FKH_TEST_DATABASE_URL`（CI），
否则用 pgserver 起本地实例。两条路径跑的是同一批迁移与同一批断言。

## 4. 快速开始

```bash
cd H:/MyWork/Factory-Knowledge-Hub
uv sync --all-groups          # 创建 .venv（Python 3.12）并锁定依赖
uv run pytest -q              # 363 项：契约 + ACL + 状态机 + 解析 + 入库 + 审核 + 检索 + 混合召回 + 培训
uv run uvicorn fkh.api.main:app --port 8000   # 启动 API 骨架
```

健康检查：

```bash
curl -s http://127.0.0.1:8000/health/live
curl -s http://127.0.0.1:8000/health/ready     # 未配置 FKH_DATABASE_URL 时返回 503
```

## 5. 数据库迁移

迁移按编号顺序执行，每个文件都提供同名回滚脚本。**不要在未知现有表结构上直接对生产库执行。**

```bash
# 需要 PostgreSQL 16+，0002 需要 pgvector
for f in migrations/0*.sql; do psql "$FKH_DATABASE_URL" -v ON_ERROR_STOP=1 -f "$f"; done
```

| 文件 | 内容 | 回滚 |
|---|---|---|
| `0000_extensions.sql` | 扩展与兼容层 | `rollback/0000_down.sql` |
| `0001_core_schema.sql` | 核心治理表、复合外键、不变量触发器 | `rollback/0001_down.sql` |
| `0002_vector_index.sql` | pgvector 列与 HNSW/GIN 索引 | `rollback/0002_down.sql` |
| `0003_training.sql` | 新人培训增量表 | `rollback/0003_down.sql` |
| `0004_rls.sql` | 全表 RLS enable + force 与策略 | `rollback/0004_down.sql` |
| `0005_content_uniqueness.sql` | 同文档内容哈希唯一约束 | `rollback/0005_down.sql` |
| `0006_outbox_delivery.sql` | outbox 租约 / 令牌 / 重试 / 死信 / 顺序键 + 重放审计表 | `rollback/0006_down.sql` |
| `0007_evidence_access.sql` | 证据定位 `required_scope` + 访问审计表 | `rollback/0007_down.sql` |
| `0008_retrieval_queries.sql` | 检索审计（HMAC 摘要、状态、返回数） | `rollback/0008_down.sql` |

L1 第 5.2 节标注「DDL 实施前必须补强」的六项，已在 `0001`/`0002` 中落实：

1. 所有引用 `principals` 的外键改为 `(tenant_id, id)` 复合约束；
2. `rag_queries` / `rag_citations` / `bad_cases` 统一复合外键，封闭跨租户引用；
3. `knowledge_chunks.parent_chunk_id` 由触发器校验同租户、同版本、且父块 `chunk_kind='parent'`；
4. `knowledge_versions` 的状态迁移、审批人、不可变内容由触发器约束，不只依赖 CHECK；
5. `lexeme` 由应用层中文分词适配器写入（`fkh.retrieval.tokenize`），数据库侧只负责 `simple` 配置承载；
6. `vector(1024)` 视为 BGE-M3 候选维度，换模型必须新建列或迁移，不允许混存。

## 6. Phase 0 门禁

实测结果（2026-11-04，本地 pgserver PostgreSQL 16.2 + pgvector，`uv run pytest -q` 全绿 363 项，其中 230 项依赖真实数据库、0 skip）：

| 门禁 | 证据 | 状态 |
|---|---|---|
| 应用层 ACL：Deny 优先、缺省拒绝、策略缺失拒绝、跨租户拒绝 | `tests/security/test_policy_engine.py` | ✅ 通过 |
| 混合权限父块不得整体恢复；未命中兄弟块受限即降级 | `tests/security/test_parent_acl.py` | ✅ 通过 |
| 片段权限不得宽于父文档 | 同上 | ✅ 通过 |
| 策略 JSON 解析严格性：未知键/维度/类型即报错 | 同上 + 数据库真实 jsonb 校验 | ✅ 通过 |
| 跨租户 SELECT 返回零行 / 仅本租户 | `tests/security/test_tenant_isolation.py`（9 项） | ✅ 通过 |
| 跨租户 UPDATE / DELETE 影响 0 行 | 同上 | ✅ 通过 |
| 跨租户 INSERT 被 WITH CHECK 拒绝 | 同上 | ✅ 通过 |
| 无租户上下文 fail-closed | 同上 | ✅ 通过 |
| 跨租户主体 / 引用复合外键拦截 | 同上 | ✅ 通过 |
| 状态机非法迁移被拒，且合法链路可走通 | `tests/security/test_governance.py`（12 项） | ✅ 通过 |
| 公开审批需两人且不同；C1 不得公开 | 同上 | ✅ 通过 |
| 版本内容不可变、不可删除、审计追加式 | 同上 | ✅ 通过 |
| 父子块同版本、父块类型与 ACL 边界 | 同上 | ✅ 通过 |
| 撤销为终态，不可复活 | 同上 | ✅ 通过 |
| 学员不得自签核；课程发布需已审批知识 | `tests/security/test_training_rules.py`（7 项） | ✅ 通过 |
| 撤销知识联动课程 NEEDS_REVIEW | 同上 | ✅ 通过 |
| 契约 Schema 合法性与正反例 | `tests/contract/test_answer_contract.py`（28 项） | ✅ 通过 |
| API 认证 / 提权 / 未实现 / 错误格式 | `tests/contract/test_api_contract.py` | ✅ 通过 |
| OpenAPI 与代码路由完全一致 | 同上 | ✅ 通过 |
| 迁移可重放、可回滚、可重建 | `migrations/apply.py --rollback 5` 实测 | ✅ 通过 |
| 解析：表格列名/单位保留、无表头与合并单元格被标记 | `tests/parsing/`（22 项） | ✅ 通过 |
| 切片：不超 token 上限、不跨分组混切、child 指回 parent | `tests/chunking/`（15 项） | ✅ 通过 |
| 状态机：应用层与数据库在 42 个迁移组合上完全一致 | `tests/security/test_state_machine_parity.py` | ✅ 通过 |
| 入库：版本与审计、父子引用、中文检索、重复内容、跨租户 | `tests/e2e/test_ingestion_flow.py`（10 项） | ✅ 通过 |
| 审核：全链路、双签拒绝、C1 不得公开、撤销即时生效 | `tests/e2e/test_review_flow.py`（10 项） | ✅ 通过 |
| 审核 API：权限分层、公开审批额外权限、错误码不泄露存在性 | `tests/e2e/test_review_api.py`（8 项） | ✅ 通过 |
| 检索：入库到检索的真实链路（跨 lane 接缝） | `tests/e2e/test_ingest_to_search.py`（6 项） | ✅ 通过 |
| 上下文：父块恢复、混合权限降级、预算控制 | `tests/e2e/test_context_assembly.py`（8 项） | ✅ 通过 |
| 引用核验：编造引用转人工、无证据拒答、响应合契约 | `tests/security/test_citation_verification.py`（15 项） | ✅ 通过 |
| outbox：租约/令牌互斥、过期后旧 worker 确认影响 0 行、业务角色零行可见 | `tests/workers/test_outbox_relay.py`（24 项） | ✅ 通过 |
| outbox：同聚合按 (版本, ordinal) 串行，退避/死信阻塞后继，第 8 次失败进死信 | 同上 | ✅ 通过 |
| outbox：发布后撤销产生两条有序事件，顺序键不被唯一约束拒绝 | `tests/e2e/test_outbox_ordering.py` | ✅ 通过 |
| 证据定位：五种拒绝同码同 body、状态/组合/scope/ACL 全链路 | `tests/e2e/test_evidence_api.py`（22 项） | ✅ 通过 |
| 证据定位：直接请求 parent 不得绕过子块可见性；locator 不泄露存储路径 | 同上 | ✅ 通过 |
| 证据定位：成功与拒绝均落访问审计，跨租户不可读，审计失败不返回正文 | 同上 | ✅ 通过 |
| 检索：不可见块（Deny/无匹配/空策略/跨租户）不出现在结果里 | `tests/e2e/test_search_api.py`（17 项） | ✅ 通过 |
| 检索：parent 不得作为普通命中；分词器不符不命中；`& ! ( )` 不破语法 | 同上 | ✅ 通过 |
| 检索：候选挤占允许不足额但不超上限；空结果与全拒绝同结构；片段≤400 码点且不泄露 | 同上 | ✅ 通过 |
| 检索：成功/空结果/范围拒绝/执行失败均落审计；审计失败不返回结果；query 明文不进响应与日志 | 同上 | ✅ 通过 |

## 7. 解析与切片（Phase 1 已完成部分）

已支持 Markdown / CSV / HTML / DOCX / 可提取文本的 PDF，产出带页码、块序号、坐标与
标题路径的块序列，再切成父子切片。

三条实现取舍值得记住：

1. **PDF 用 pdfplumber 而非 PyMuPDF**。后者是 AGPL-3.0，闭源商业交付会带来许可传染风险；
   pdfplumber 在 MIT 下同样能给坐标与表格。
2. **扫描件不静默降级**。没有 OCR 就标记 `PARSER_FALLBACK` 进人工复核队列，
   而不是把空文本当作"解析成功"。OCR 引擎待 ADR-05 签署。
3. **parent 按一二级标题分组，不按所有标题切**。实测同一份契约文档，逐标题切会产生
   59 个 parent、中位数 132 token；按一二级分组后为 17 个、中位数 502 token。

完整规格、已知限制与实测数据见 [`docs/SPEC/parsing-and-chunking.md`](./docs/SPEC/parsing-and-chunking.md)。

尚未完成（下一阶段）：

- outbox 投递器已落地（`src/fkh/workers/outbox_relay.py` + `0006`，规格见
  [`docs/SPEC/outbox-delivery.md`](./docs/SPEC/outbox-delivery.md)）；仍未做：消费者、
  告警通道接线、上游真实 trace 贯通、运维页面；
- 证据定位已落地（`GET /v1/evidence/{id}` + `0007`，规格见
  [`docs/SPEC/evidence-locator.md`](./docs/SPEC/evidence-locator.md)）；仍未做：签名 URL（待 ADR-01）、
  引用级独立失效、**存量版本 `required_scope` 赋值**（未赋值的版本一律 403，属有意 fail-closed）；
- 草稿终止路径（`DRAFT` 无出口，待 L1 裁决，见 `docs/REVIEW-REQUEST-draft-termination.md`）；
- OCR 通道（阻塞于 ADR-05）；
- 真实 tokenizer 替换近似计数（阻塞于 ADR-04/06 的模型选择）；
- 词法检索已落地（`POST /v1/knowledge/search` + `0008`，规格见
  [`docs/SPEC/retrieval-lexical.md`](./docs/SPEC/retrieval-lexical.md)）；仍未做：向量检索、
  rerank、分页、候选挤占缓解（L1 已明确接受该代价）；
- 应用层 ACL 已接入检索链路；向量候选过滤与混合排序待 ADR-04/06；
- 撤销后缓存与索引 generation 收敛验证（Phase 2）；
- 真实数据的金标准集与坏例回归（Phase 2，依赖 ADR-08）。

P0 未全绿时不得宣告 Phase 0 完成，也不能用平均分掩盖单点失败。

## 7. 明确不做

V1.0 不做 ERP/MES 实时业务事实存储，不替代图纸管理/PDM，不提供自动报价或邮件自动发送，不允许模型直接控制生产设备，不把 Obsidian Vault 当共享盘，不把未审核转录稿作为对外答案依据，不做薪资绩效、人事主档、自动颁发法定资质、AI 自主评定实操合格。
