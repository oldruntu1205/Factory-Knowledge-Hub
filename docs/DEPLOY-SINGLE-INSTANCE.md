# 两套实例的启动说明与配置差异（2026-11-08）

> 同一提交、两个隔离环境；本文件是"换配置就能给另一个客户用"的操作面，
> 验收见 `tests/e2e/test_two_instances.py`。

## 1. 起一套实例（最小步骤）

```powershell
# 1) 依赖与代码（两套实例同一个 commit）
uv sync

# 2) 每套实例独立的数据目录与端口
.\scripts\instance-up.ps1 `
    -Name factory `
    -PgData .instance-factory `
    -DbPort 55441 -ApiPort 8101 -UiPort 8111 `
    -Provider ollama -Model qwen3:14b

# 3) 打开 UI
#    http://127.0.0.1:8111/
```

`scripts\instance-up.ps1` 会：建/复用该实例的 PG 数据目录 → 起内嵌 PG → 应用迁移 →
起 API + 演示 UI（同源代理）→ 打印该实例的 tenant/subject。

## 1a. 实测启动结果（2026-11-08）

```
.\scripts\instance-up.ps1 -Name smoke -PgData .instance-smoke `
    -DbPort 55461 -ApiPort 8103 -UiPort 8113 -Provider none
```

输出（节选）：

```
-> 0030_backfill_all_generations.sql
已应用 30 个迁移。
[demo] 播种完成：subject='077eb693@demo.local', chunks=198, items=1252, conflicts_open=29
[demo] API 启动中 127.0.0.1:8103
[instance:smoke] 就绪：http://127.0.0.1:8113/  subject=077eb693@demo.local
```

对比第一套（长期在跑的演示实例）：`subject=648b7644@demo.local`。**两套实例的 subject/数据完全独立**，
第二套是从同一提交新建库、跑完全部 30 个迁移后自动播种出来的。

**踩到的坑（已修进脚本）**：不能预先创建 `$PgData` 目录——`initdb` 要求目录为空或不存在；
日志也不能写进 `$PgData`，否则 `initdb` 直接报 `directory exists but is not empty`。
脚本现在把日志放到 `$PgData-logs`。

## 2. 第二套实例（不同行业）

```powershell
.\scripts\instance-up.ps1 -Name roaster -PgData .instance-roaster `
    -DbPort 55442 -ApiPort 8102 -UiPort 8112 -Provider ollama -Model qwen3:14b
```

两套实例的差异**只在配置**：

| 配置项 | 实例 A（工厂） | 实例 B（咖啡烘焙设备） |
|---|---|---|
| PG 数据目录 | `.instance-factory` | `.instance-roaster` |
| 数据库端口 / API / UI | 55441 / 8101 / 8111 | 55442 / 8102 / 8112 |
| `FKH_JWT_SECRET` | 独立值 A | 独立值 B |
| 导入资料 | 目录（货号 A1124xx）+ 检验规范 | 目录（货号 B2203xx）+ 保养手册 |
| 知识域 | `product_catalog` / `quality` | `product_catalog` / `equipment_safety` |
| Agent（用途） | 客服（`customer_public`） | 维护（`internal_product`） |
| 代码 | 同 commit | 同 commit（未改一行） |

**没有改动的部分**：检索（词法/混合/目录精确）、可见性谓词、引用与证据、撤销与索引闸门、
培训/访谈链路、审计与 outbox。差异全在"配置 + 导入的资料 + 接入的 Agent 身份"。

## 2a. 双实例实机验收（2026-11-08，真实进程）

**两套实例同时在跑（同一提交 `b4d093a` + 本轮代码）**：

| | 实例 A（工厂） | 实例 B（烘焙设备） |
|---|---|---|
| 启动 | 已长期运行的演示实例（`.demo-pgdata`） | `instance-up.ps1 -Name roaster -PgData .instance-roaster -DbPort 55462 -ApiPort 8104 -UiPort 8114 -Corpus _corpus_roaster/roaster_catalog.md -SkipTrainingSeed` |
| 迁移 | 33 个 | **33 个（新库从零迁移）** |
| 资料 | 工厂目录 + 检验规范 + 培训 | **只有 `_corpus_roaster/roaster_catalog.md`（4 chunks / 2 目录条目）** |
| tenant | `f101e87c-…` | `085f023e-…` |
| subject | `648b7644@demo.local` | `f53f141e@demo.local` |

**最小权限 Agent 令牌**：`scope=[knowledge.answer, knowledge.search, evidence.read]`，
`allowed_scopes=[internal_product]`（其余 scope 一律不给；两实例各自用自己的密钥签发）。

实测输出（`_evidence/two_instances_live.txt`，脚本 `scripts/two_instance_acceptance.py`）：

```
A_answer            200 ANSWERED "A112420 的单价为 39.00（CNY）…"  citations=1
B_answer            200 ANSWERED "烘焙机银皮收集盒每烘焙 50 锅清理一次…"  citations=1
B_factory_question  200 REFUSED        ← B 没有工厂资料
A_token_on_B        401 UNAUTHENTICATED
B_token_on_A        401 UNAUTHENTICATED
A_revoke            200（撤销 A 的目录版本）
A_after_revoke      200 REFUSED        ← 单边撤销立刻生效
B_after_A_revoke    200 ANSWERED       ← B 完全不受影响
verdict             pass=true
```

> 复现命令：`uv run python scripts/two_instance_acceptance.py`（需要两套实例分别在 8000/8010 与 8104/8114 运行）。

## 3. 验收结果（`test_two_instances.py`，自动化）

| 场景 | 结果 |
|---|---|
| A 工厂资料可答 / B 烘焙手册可答 | 2 条均 `ANSWERED`，答案来自各自资料 |
| A 的 JWT 打 B | **401**（密钥不同） |
| A 的数据库角色连 B 的库 | 查询被 PG 拒绝（无权限） |
| A 撤销自己的检验规范 | A 不再能答；**B 仍正常作答** |
| 代码差异 | 无（同一 `create_app`） |

## 4. 实际阻碍复用的地方（本轮实测）

| # | 阻塞点 | 证据 | 建议 |
|---|---|---|---|
| 1 | ~~知识域枚举写死~~ | **已解决**（2026-11-08，`0035`）：`knowledge_domains` 实例配置 + 复合外键；未登记仍 422 但可自助登记（`POST /v1/config/domains`）。实机证据 `_evidence/domain_config_live.txt` | 明细见 `docs/SPEC/domain-config.md` |
| 2 | ~~目录货号正则写死~~ | **已解决**（0036）：`catalog_rules.code_pattern` 实例配置；`provision --catalog-pattern` 可首装设置 |
| 3 | ~~目录货币只允许 CNY~~ | **已解决**（0036）：`catalog_rules.currencies`；`provision --currencies USD,EUR` |
| 4 | **商务承诺词表内置中文外贸词** | 其他行业词不会命中，靠语义兜底 | 已支持 `FKH_COMMITMENT_KEYWORDS_EXTRA` 按实例追加；内置拦截词不可删除 |
| 5 | 演示语料/页面文案含工厂样板 | `_corpus/*`、`tools/demo_*` | 属演示资产，不进内核；新客户换语料即可 |

第 1–3 项已解决；第 4 项已提供追加配置，仍保留内置安全词表（不提供删除入口）。
首装非工厂实例时用：

```powershell
uv run python -m fkh.tools.instance provision --workdir .instance-customer \
  --db-port 55501 --api-port 8151 --db-name fkh_customer \
  --domain maintenance=保养 --domain product_catalog=产品目录 \
  --catalog-pattern '^SKU-[0-9]{4}$' --currencies USD,EUR
```
