# L1 复审：一致性检查的两项整改（`5f723a2`）

- 日期：2026-11-07
- 对应终审意见：`_evidence/l1_reply_consistency_final_20261107.md`（CHANGES_REQUESTED 两项）
- 请只做判断：**不要探索仓库、不要读源码、不要跑测试**。

## 1. 整改一：区间/尺寸的单位必须可确定且一致

原实现"结构体出现即支持"已删除。现在的判定：

1. 先比**完整字面**（`24×24mm` 必须原样出现，带边界）；
2. 不中时，对区间/尺寸退化为结构体匹配，但要求：
   - 在证据里定位含该结构体的**那一行**；
   - 取"该行 + 上一行（表头/邻近行）"作为上下文；
   - **上下文里必须出现 claim 声明的同一个单位**（`mm` 就是 `mm`）；
   - 单位缺失（表头也没写）→ **不支持**；单位不同（表头 `mm`、claim `cm`）→ **不支持**。

新增三条断言（都已通过）：

| 证据 | claim | 期望 | 实际 |
|---|---|---|---|
| `货号 规格 管径*总长 上标口*下标口 单价` / `A112420 200mm 38*300 24*24 39.00` | `24×24mm` | 支持 | ✅ |
| 同上 | `24×24cm` | 不支持 | ✅ |
| `上标口*下标口 24×24`（无任何单位） | `24×24mm` | 不支持 | ✅ |

区间（`14-70`）走同一段逻辑：数值结构相同但单位不可确定时同样判不支持。

## 2. 整改二：类别码显式记录

- `ClaimCheck.reason_codes` → `("UNSUPPORTED_CODE", "UNSUPPORTED_NUMBER")`（按命中类别）；
- 服务日志改为 `answer unsupported claims: reasons=UNSUPPORTED_CODE,UNSUPPORTED_NUMBER codes=1 numbers=2 trace_id=...`；
- 普通调用方仍只回 `unsupported={codes,numbers}` 数量，不回原文、不逐条落库；
- 规则版本升为 **`literal-support-v2`**（口径变了，审计要能区分 v1/v2）。

## 3. 门禁与实测

```
uv run pytest -q                → 506 passed
uv run pytest -q -m requires_pg → 306 passed, 197 deselected, 0 skip
ruff / mypy                     → 全绿
```

单测 14 项（含新增 3 项单位三态 + 2 项类别码），e2e 4 项（支持/不支持/不适用/审计字段，
规则版本断言已改为 v2）。

## 4. 未变的部分（上一轮你已接受）

- 只跟有效引用的证据块比对；
- 未支持 ⇒ `HUMAN_REVIEW_REQUIRED` + `UNSUPPORTED_CLAIM`，保留答案与引用；
- 无 claim 记"不适用"（`NULL`），不记 100%；
- `tax-04` 不在本切片覆盖范围，仍未解决。

请裁决：**PASS** / **CHANGES_REQUESTED（逐条）**。
