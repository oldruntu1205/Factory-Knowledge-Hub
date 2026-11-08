# L1 终审：KEEP 决议收敛（0031–0033）+ 双实例实机验收（`c581614`）

- 日期：2026-11-08
- 风险归类：**DB 迁移**、**撤销不可逆性**、**审计完整性**、部署边界验证
- 请只做判断：**不要探索仓库、不要读源码、不要跑测试**。

## 1. KEEP 决议 + 撤销的真实复现链

| 阶段 | 实测错误 | 修法（均为**新迁移**，不改已发布文件） |
|---|---|---|
| 清理 1 | `delete on catalog_conflict_candidates violates foreign key constraint "fk_group_selected_candidate"`（0015 是 RESTRICT） | `0032` 改为列级 `ON DELETE SET NULL (selected_candidate_id)` |
| 清理 2 | `catalog_conflict_groups_check` 要求 KEEP 必有 `selected_candidate_id`，置空后撞 CHECK | `0033` 放宽为"有候选 **或** 有决议快照" |
| 审计 | 候选被级联删除后，决议只剩悬空 UUID | `0031` 决议时写 `resolved_snapshot`（候选 ID/原文/规格/价格/定位/操作者/备注），并回填历史 KEEP |

验收（`tests/e2e/test_catalog_keep_revoke.py`，真实 PG + 业务角色 + HTTP）：
KEEP 后目录可答（39.50，引用指向该版本）→ **撤销后旧证据立即 403** → 清理完成
（块/目录条目/候选计数均 0）→ 冲突组仍是 KEEP、`selected_candidate_id` 为空、
`resolved_snapshot` 含 `spec`/`raw_price`/`resolved_by`/`note`。

## 2. 双实例实机验收（真实进程，非测试桩）

同一提交启动两套实例：
- A（工厂）：长期演示实例，tenant `f101e87c…`，subject `648b7644@demo.local`
- B（烘焙设备）：`instance-up.ps1 -Name roaster -PgData .instance-roaster -DbPort 55462 -ApiPort 8104 -UiPort 8114 -Corpus _corpus_roaster/roaster_catalog.md -SkipTrainingSeed`，
  **新库从零跑完 33 个迁移**，4 chunks / 2 目录条目，tenant `085f023e…`

各自**最小权限 Agent 令牌**（`scope=[answer,search,evidence.read]`，`allowed_scopes=[internal_product]`）：

```
A_answer            200 ANSWERED  citations=1
B_answer            200 ANSWERED  citations=1
B_factory_question  200 REFUSED
A_token_on_B        401 UNAUTHENTICATED
B_token_on_A        401 UNAUTHENTICATED
A_revoke            200（撤销 A 的目录版本）
A_after_revoke      200 REFUSED
B_after_A_revoke    200 ANSWERED  citations=1
verdict             pass=true
```

证据：`_evidence/two_instances_live.txt`，脚本 `scripts/two_instance_acceptance.py`。

## 3. 需要你判断的四点

1. **0033 放宽 CHECK** 为"有候选 **或** 有快照"。这是否足够严格？
   我理解快照与候选等价承载决议事实，但快照是应用层写入的 jsonb，库层无法验证其完整性。
   若要求库层强校验（例如快照必含 spec/raw_price 键），需要写 CHECK on jsonb。
2. **0032 删除 0015 的 RESTRICT 约束**：这意味着"选中候选"不再被数据库强引用。
   在"撤销必须收敛"与"决议必须可审计"冲突时，我选择后者由 `resolved_snapshot` 保证。
   是否接受这一取舍？
3. **双实例验收的最小权限 Agent** `allowed_scopes=[internal_product]` 仍能读到该实例所有内部知识。
   单实例私域形态下是否需要更细的 Agent 级范围（按用途拆 scope，如 `maintenance`/`sales`）？
   本片未做，属产品口径。
4. **A 的实例携带历史演练数据**（多份合成目录）。本轮撤销了 A 的全部目录版本以验证单边撤销；
   这是否影响你对"A_answer 首答"这步证据的接受度（我已在文档中注明）？

## 4. 门禁

```
uv run pytest -q            → 629 passed   exit=0
uv run ruff check src tests → All checks passed!  exit=0
uv run mypy                 → Success: no issues found  exit=0
```
