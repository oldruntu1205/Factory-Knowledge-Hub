# L1 终审：对客可见性 P0 纠偏（审计基准 7e9681a）

- 日期：2026-11-07
- 风险归类：**鉴权/权限**、**DB 迁移**、**对外语义变化**
- 请只做判断：**不要探索仓库、不要读源码、不要跑测试**。

## 1. 审计认定的事实（已复现）

`find_catalog_item` 的可见性只查 `status IN ('APPROVED_INTERNAL','PUBLISHED_PUBLIC')` + `required_scope`，
**没有把 audience 与审批状态配对**。合成 C0 目录（`audience=customer_public`）在只做了技术审批 +
内部发布（`APPROVED_INTERNAL`）、**尚未独立公开审批**时，客户令牌（`allowed_scopes={"customer_public"}`）
问「A112420 的单价是多少？」即返回：

```
ANSWERED  A112420 的单价为 39.00（CNY），规格：200mm 38*300 24*24   （带 1 条真实引用）
```

普通检索与证据读取本来就按 `APPROVED_INTERNAL+internal / PUBLISHED_PUBLIC+customer_public` 配对，
所以三条读路径里**只有目录精确通道例外**——正是审计说的口径不一致。

## 2. 改动

### 2.1 统一判据（唯一来源）

`fkh.authz.policy.is_external_customer(allowed_scopes)`：`scopes == {"customer_public"}`。

可见性谓词（检索/混合/目录/证据共用同一语义）：

```
( status = 'PUBLISHED_PUBLIC' AND audience = 'customer_public' )
OR ( internal AND status = 'APPROVED_INTERNAL' )
```

- `internal` = 非外部客服（即 allowed_scopes 不只有 customer_public）。
- 即：**外部只看已独立公开审批的公开内容；内部可看 `APPROVED_INTERNAL`（不论 audience）**。
  这是按 Rev2 §3.4「外部客服仅 PUBLISHED_PUBLIC 且 audience=customer_public」与
  §4.4.2「过滤 APPROVED_INTERNAL / PUBLISHED_PUBLIC，外部客服只允许后者」的直译。

### 2.2 外部客服商务承诺转人工（Rev2 §4.5）

外部客服（`allowed_scopes == {"customer_public"}`）询问价格 / 交期 / 质量承诺 / 定制可行性时，
**在进入目录精确通道之前**就转人工，返回：

```
status = HUMAN_REVIEW_REQUIRED, refusal_code = HUMAN_REVIEW_REQUIRED, citations = []
answer = 「这类问题涉及价格、交期、质量承诺或定制可行性，需要业务人员确认后回复……」
```

关键词表是确定性的（可审计）：`单价/价格/报价/多少钱/折扣/优惠/交期/交货/货期/现货/
质保/保修/保证/承诺/包换/包退/赔偿/定制/定做/开模/改型/图纸/加工`。

### 2.3 迁移 `0018_handoff_route.sql`

`rag_queries.route` 的 CHECK 增加 `handoff`，`route_reason` 增加 `external_customer_commitment`；
回滚脚本在库中已存在 handoff 记录时**拒绝回滚**（避免审计语义丢失）。

## 3. 需要你判断的四点

1. **内部可读 `APPROVED_INTERNAL`（不论 audience）** 是否符合 Rev2？我理解为「是」：
   §4.4.2 只对外部客服限定 PUBLIC；且审核期内内部需要能读到将要公开的内容。
   若你要求「内部也不得读到尚未公开的 customer_public 内容」，我改成严格配对（internal 也要 audience 匹配）。
2. **外部客服判定用 `allowed_scopes == {"customer_public"}`** 是否足够？现有令牌模型里没有
   "渠道/身份类型"声明，我用数据范围反推。若要求显式 claim（如 `channel=external_service`），
   这是契约变化，需要你确认后再加。
3. **承诺类关键词表**是否够用？我刻意选了保守的词表，但漏词会导致漏拦（价格写成"什么价"、
   "几号能到"）。是否要求同时保留"命中即转人工 + 未命中但疑似商务问题由模型判断"的双层？
   我倾向**只用确定性词表**（可解释、可回归），漏词风险由词表迭代和坏例集吸收。
4. **迁移**：只放宽两个 CHECK 取值域，不重写历史行。是否接受？

## 4. 已做的验证（真实 PostgreSQL + 四个受限身份 + 实际 HTTP）

`tests/e2e/test_customer_visibility.py`（新增，5 条，全部通过）：

| 场景 | 结果 |
|---|---|
| 技术审批 + 内部发布后，客户问单价 | 不返回 ANSWERED，答案不含 `39.00` |
| 同状态下客户检索该货号 | 结果里没有该版本 |
| 同状态下客户直读该版本块证据 | 403 |
| 独立公开审批后，客户问规格 | ANSWERED，带引用 |
| 客户问单价/多少钱/交期/保证/定制 | 全部 `HUMAN_REVIEW_REQUIRED`，答案不含价格 |
| 公开后撤销，客户再问 / 检索 / 旧引用直读 | 三路立即失效（403 / 空结果 / 非 ANSWERED） |
| 内部身份问同一货号单价 | ANSWERED，价格正确（未误伤） |

修复前后对比（同一测试文件）：`3 failed / 2 passed` → `5 passed`。

全量门禁：`561 passed`，`ruff` / `mypy` 全绿。

## 5. 明确未做 / 未实测

- 未改检索排序与召回；未改 `SUPERSEDED`、`SOURCE_REVOKED` 等其它拒绝码的触发条件；
- 演化中的 `PUBLISHED_PUBLIC` 又取消发布的路径（状态机无该转换）未涉及；
- 承诺词表只覆盖中文；英文外贸询价（`price / lead time / MOQ`）**尚未纳入**，需要你确认是否本片补；
- 前端演示代理未改（它只是演示证据，不是安全边界）。
