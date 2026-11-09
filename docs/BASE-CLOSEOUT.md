# RAG 基座 V1 收尾记录

- 日期：2026-11-09
- 基线：`767df24` + `9963980`（契约清理 / 实例配置参数化 / 承诺词表追加）
- 定位：**单实例私域、可复用 RAG 基座**（ADR-009）；不是客户验收报告，不做 SaaS/计费。

## 1. 收尾判断

| 口径 | 结论 |
|---|---|
| 工程代码冻结 | ✅ 可以：V1 能力闭环、门禁全绿、高风险切片有 L1 证据 |
| 开箱即用的通用行业产品 | ⚠️ 有条件：实例域/货号规则/币种/承诺词已可配置，但行业默认值仍按工业/工厂语料给出 |
| 客户验收版 | ❌ 不在本收尾范围：需客户 50–100 条真实问题 + recall@10 ≥0.70 + 描述类问答质量确认 |

## 2. V1 能力边界

### 可交付

- 入库：文档上传、解析、父子切片、审批/发布/撤销/弃用；
- 权限：租户/ACL/scope、证据定位与访问审计、请求级快照；
- 检索：默认 `lexical-v3`（BM25），可选 `hybrid`（服务端显式 opt-in，生产未授权）；
- 目录：货号精确查询（不经模型）、冲突待办与人工决议；
- 问答：本地模型 + 引用核验 + 一致性检查；无证据不调模型；
- 培训：课程、分配、问答、测验、签核、反馈；
- 信任：JWT 轮换、业务角色最小权限、实例隔离、审计；
- 性能：10 万切片统计有效时 p50 579ms / p95 1.48s；失效回退 live。

### 不做 / 不承诺

- 描述类问答对客准确率（当前 14/17 且会摆动）；
- 外部 LLM/跨境调用（ADR-04 未签）；
- 真实 ASR、录音保留期作业（ADR-05 未签）；
- 多租户 SaaS、跨实例检索/统一登录、计费（ADR-009 排除）；
- 生产级并发/SLO 验收（ADR-06 未签）；
- `/v1/sources` 与 `/v1/knowledge/drafts`：V1 不提供，已从公开契约与路由移除；
  调用返回 **404**（不再是永久 501）。迁移替代：导入用 `POST /v1/knowledge/documents`，
  草稿由上传/`POST /v1/interviews/drafts/{id}/promote` 产生。

## 3. 基座配置入口

```powershell
# 非工厂实例首装：域、货号正则、币种一次设定
uv run python -m fkh.tools.instance provision --workdir .instance-customer \
  --db-port 55501 --api-port 8151 --db-name fkh_customer \
  --domain maintenance=保养 --domain product_catalog=产品目录 \
  --catalog-pattern '^SKU-[0-9]{4}$' --currencies USD,EUR

# 外部客服商务承诺词：只追加，不删除内置安全词
$env:FKH_COMMITMENT_KEYWORDS_EXTRA = "巡检周期,保养周期,inspection interval"
```

追加词规则：逗号拆分、去首尾空白、转小写；空项忽略；重复不报错；内置词始终保留。
它只是**关键词加严**，不能识别所有商务承诺表达；未命中时仍由语义分类器 fail-closed 兜底。

## 4. 验收命令

```powershell
uv run pytest -q                                  # 740 passed
uv run ruff check . ; uv run mypy                 # 全绿
uv run python -m fkh.tools.release_check --url "$FKH_DATABASE_URL" --provider "$FKH_LLM_PROVIDER"

# 统计刷新：发布/撤销后异步重建（compose stats-refresher 或）
uv run python -m fkh.retrieval.stats --url "$FKH_DATABASE_URL" --watch --interval 5
uv run python -m fkh.tools.instance status --workdir .instance-customer
```

测试库 `release_check` 最近结果：**12 PASS / 1 WARN / 0 FAIL，GO**；WARN 是
`FKH_LLM_PROVIDER=none`（描述类 503，货号/报价不受影响）。

## 5. 客户验收包（不在本收尾内，部署时执行）

1. 用 `docs/SPEC/customer-recall-cases.template.json` 收 **50–100 条**客户真实问题；
2. `scripts/validate_recall_cases.py` 校验后，`fkh.tools.recall_benchmark` 跑 lexical/hybrid；
3. 门槛：recall@10 ≥ **0.70**；不足时先调语料/分词/混合检索，不宣称已验收；
4. 描述类问答单独做人工复核验收；未通过前只开放货号/报价与冲突治理；
5. 并发/冷缓存性能与 ADR-06 SLO 另做，不拿单查询基准代替。

## 6. 已知残留

- ADR-01/02/03/04/05/06/07/08 仍有未签项；系统按“未签时 fail-closed”运行；
- 统计刷新失败期间回退 live（10 万约 2.7s）；`stats-refresher` 必须被进程守护实际拉起；
- 旧实例业务角色未授予 0037 三表 SELECT 时回退 live，功能不受影响但拿不到低延迟；
- 前端仅演示 UI，不是交付级前端；培训/访谈无独立验收脚本；
- 备份为逻辑备份，未验证物理备份/WAL 连续归档。

## 7. 回滚

```powershell
git revert <收尾提交>
# 统计表单独回退（可选；回退后检索自动 live）
uv run python migrations/apply.py --url "$FKH_DATABASE_URL" --rollback 1
```

契约移除的回滚：恢复 `main.py` 的 `CONTRACT_ROUTES` 与 OpenAPI 两段即可；V1 客户端
不应依赖 501 路径。


