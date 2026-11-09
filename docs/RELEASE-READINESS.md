# Factory Knowledge Hub · 交付边界与上线清单

- 日期：2026-11-07
- 适用范围：三爱思产品目录（`Catalog_2023.pdf`，85 页 / 1,252 条目录条目）
- 一句话结论：**「查货号/报价」与「冲突治理」可以交付；「描述类问答」当前不可交付。**
- 本清单只写能被数字或命令验证的内容；无法验证的一律标"未验证"。

---

## 一、能交付的部分

### 1. 货号 / 报价查询（结构化通道）

| 项 | 事实 |
|---|---|
| 测量 | 20 条货号询价：**ANSWERED 20/20、价格全对、各 1 条引用** |
| 延迟 | 中位 **0ms**（确定性查询，不经过任何模型） |
| 冲突货号 | 决议前一律 `REFUSED/NO_EVIDENCE`（抽检 5/5 拒答） |
| 证据 | `_evidence/catalog_acceptance_20261106.md` |

可用性前提：目录文本已入库并审批为 `APPROVED_INTERNAL`，抽取产生的 `catalog_items` 非空。

### 2. 冲突复核（人工裁决闭环）

| 项 | 事实 |
|---|---|
| 真实目录 | 29 组待办 / 62 条候选 |
| 决议 | `KEEP <candidate> | DROP_ALL`，一次决议不可改（错判走新版本） |
| 重抽取 | 决议**不被自动流程改写**（演练：29→27 组、62→58 候选，状态保持） |
| 导出 | `export --format md|csv`，只读；含价格的文件落 `_corpus/`（不入库、不进仓库） |
| 证据 | `_evidence/catalog_ops_drill_20261106.md` |

### 3. 检索与引用

| 项 | 事实 |
|---|---|
| 词法排序 | BM25 字级重排（`lexical-v3`），冻结用例 recall@10 **0.48 → 0.62**（+8 / −1） |
| 引用 | 答案带系统侧 `evidence_id/version_id/locator`；展示层把内部 id 转 `[1]/[2]` |
| 一致性检查 | `literal-support-v4`（2026-11-07）：货号/带单位数值必须字面出现在被引用证据中，否则转人工；单位白名单已补压力/时间/倍数/电功率等工业量纲（v3 曾漏掉 `0.2MPa`/`60秒`/`1.5倍`，实测出现过编造值被判 ANSWERED）；耗时 0–1ms |
| 证据 | `_evidence/lexical_v3_benchmark_20261106.md`、`_evidence/answer_consistency_20261107.md` |

---

## 二、不能交付的部分（照实说）

### 描述类问答

| 指标 | 现状 |
|---|---|
| 回归通过率 | **14/17**（`--repeat 2`，每条用例每次都必须过） |
| 稳定性 | 同一题会在 `ANSWERED / REFUSED / HUMAN_REVIEW_REQUIRED` 之间摆动（实测） |
| 典型失败 | 把「标口 14-70mm」答成 `24*24`；语料无税费信息时答「不含税」；有证据也拒答 |

原因不是链路缺环，而是 **本地 14B 的能力边界**：读不动裸表行（已改写成 `货号X：列名=值` 缓解，
但仍不稳）、会用常识补证据空白、引用行时有时无。

**结论：描述类问答当前只适合"内部试用 + 人工复核"，不适合作为对外交付能力。**

### 其他未完成项

- ADR-04 未签署 ⇒ 只能接本地模型；`FKH_LLM_PROVIDER=none` 时描述类返回 **503**（货号/报价不受影响）。
- 大租户检索：5000 块规模中位 142ms；**10 万块基准 p50 2.71s / p95 3.63s**（单查询、非并发，候选 SQL 1.32s + 统计 1.43s）；统计预计算与候选优化均未做（`_evidence/retrieval_perf_20261109.md`）。
- 一致性检查抓不到"价格不含税"这类没有货号/数值的断言（L1 已明确此边界）。

---

## 三、上线清单（逐条可勾选）

```bash
# 0. 代码与迁移
git log --oneline -1                       # 期望 3088d46 之后
uv run python -m migrations.apply --url "$FKH_DATABASE_URL"   # 期望：没有待应用迁移（0001–0017）

# 1. 上线自检（任一 FAIL 即 NO-GO）
uv run python -m fkh.tools.release_check --url "$FKH_DATABASE_URL" --provider "$FKH_LLM_PROVIDER"

# 2. 门禁
uv run pytest -q                           # 期望 509 passed
uv run pytest -q -m requires_pg            # 期望 306 passed / 0 skip
uv run ruff check src tests migrations
uv run mypy

# 3. 语料与目录
#    · 入库并审批（APPROVED_INTERNAL）
#    · 目录抽取：catalog_items > 0，无冲突货号才可直接作答
#    · 导出待办清单交运营
uv run python -m fkh.tools.catalog_conflicts --url "$FKH_DATABASE_URL" \
  export --tenant <uuid> --format md --out ./catalog_conflicts_open.md

# 4. 回归基线（用于上线后对比，不作为 GO 条件）
uv run python -m fkh.tools.answer_regression \
  --pgdata <数据目录> --corpus <语料> --cases _evidence/answer_cases_20261106.json \
  --model qwen3:14b --with-price-cases 10 --repeat 2
```

- [ ] 迁移全部应用（0001–0017）
- [ ] `release_check` 输出 **GO**
- [ ] 门禁全绿（509 / 306 / ruff / mypy）
- [ ] 语料已审批、`catalog_items` 非空
- [ ] 冲突待办已导出并交给运营（数量与库内一致）
- [ ] `FKH_LLM_PROVIDER` 明确（`none` 时对外只承诺查货号/报价）
- [ ] 审计表可读（`rag_queries` / `retrieval_queries`）
- [ ] 备份策略确认（每日全量 + WAL）

### 必填环境变量

| 变量 | 用途 | 缺失后果 |
|---|---|---|
| `FKH_DATABASE_URL` | 主库 | 启动失败 |
| `FKH_JWT_SECRET` | 令牌校验 | 401 |
| `FKH_RETRIEVAL_HASH_KEY` / `_ID` | 审计里 query 的 HMAC | 检索 503 |
| `FKH_LLM_PROVIDER` | `none` / `ollama` | 描述类 503（不伪装成拒答） |

---

## 四、验收门槛（对客户能承诺的数字）

| 能力 | 承诺 |
|---|---|
| 货号/报价 | 抽样 20 条：全部答对、各带 1 条原文引用、延迟 <10ms |
| 冲突货号 | 决议前 100% 拒答（不猜、不用旧的顶） |
| 冲突治理 | 决议后即刻生效；重抽取不改写结论 |
| 描述类问答 | **不做承诺**（当前 14/17 且不稳定） |

---

## 五、回滚

| 场景 | 动作 |
|---|---|
| 代码回退 | `git revert <commit>`（每个切片独立提交：`893a1ed` 引用协议、`1e58de2` 词法 v3、`fb14004` 表格改写、`490cdf3`+`5f723a2`+`3088d46` 一致性检查） |
| 迁移回滚 | `0017` / `0016` 有数据时**拒绝**（审计依据不可抹）；空库可回滚 |
| 生成能力故障 | `FKH_LLM_PROVIDER=none` 重启 ⇒ 描述类 503，货号/报价不受影响 |
| 检索排序异常 | 回退到 `1e58de2` 之前；`lexical-v2` 仍可读历史审计 |

---

## 六、运维手册

```bash
# 待办清单（只读）
python -m fkh.tools.catalog_conflicts --url <dsn> export --tenant <uuid> --format md --out <path>

# 决议（actor 必须是该租户活跃 user 主体）
python -m fkh.tools.catalog_conflicts --url <dsn> resolve --tenant <uuid> --version <uuid> \
  --code <货号> --keep <candidate_id> --actor <uuid> --note "以主表为准"

# 定期自检
python -m fkh.tools.release_check --url <dsn> --provider ollama
```

审计排查（不落 query 明文）：

```sql
-- 近期被降级为人工复核的回答
SELECT created_at, status, refusal_code, consistency_rule, codes_total, codes_supported,
       numbers_total, numbers_supported, consistency_ms
  FROM rag_queries
 WHERE status <> 'ANSWERED'
 ORDER BY created_at DESC LIMIT 20;
```

---

## 七、红线（已由 L1 裁决冻结，不得在下游放宽）

1. 目录精确通道**永不使用模型**；
2. 冲突决议**一次性**，错判走新版本；
3. 引用必须指向真实证据，展示层不得裸露内部 `chunk_id`；
4. 一致性检查只做**字面**核验，不得宣称"答案正确"；
5. 无证据时不调模型（`NO_EVIDENCE`），能力故障（503）不得伪装成业务拒答。

---

## 八、私域部署（改正：不需要域名）

**本项目是私域部署，不存在"域名/证书"这一环。** 需要的是：

| 要素 | 私域里的形态 |
|---|---|
| 访问地址 | 内网 `IP:8000`（或客户既有反代后面的内部路径），**不需要公网域名、不需要 TLS 证书** |
| 进程托管 | docker compose（`deploy/compose`）或 systemd；端口默认只绑 `127.0.0.1` |
| 数据库 | 内网 PG + pgvector；或客户既有 PG（需确认版本 ≥16、可装 pgvector） |
| 模型 | 本机 Ollama（loopback），模型 blob 需随包分发 |
| 备份 | 每日 `pg_dump` + WAL 归档，落在客户内网存储 |

仓库里的部署物本身已经是内网口径：`docker-compose.yml` 只 `expose` 数据库端口、
API 仅发布到 `127.0.0.1:${API_PORT}`；`Dockerfile` 不含任何域名/证书配置。

### 真正卡住私域部署的三件事（不是域名）

1. **ADR-01（部署区域与备份位置）未签署** —— `docker-compose.yml` 自己写着"签署前不得当作生产方案"。
2. **离线依赖**：`Dockerfile` 需要拉 `python:3.12-slim` 与 `ghcr.io/astral-sh/uv`，`uv sync --frozen`
   默认要联网。私域/气隙环境必须二选一：
   - 在**外网预构建镜像**，以 `docker save` 导出 tar 交付，内网 `docker load`；
   - 或预先生成 wheel 缓存，内网用 `uv sync --offline`（**未实测**，需在目标机验证）。
3. **模型分发**：`qwen3:14b` 约 9GB，不在镜像内，需要单独拷贝 Ollama 模型目录
   （或内网私有 registry 分发），并确认目标机显存/磁盘。

以上三条都需要你确认目标环境后我才能动手；在此之前我不会假装"部署已经就绪"。
