# 答案—证据一致性检查：实现与实测

- 日期：2026-11-07
- 裁决：`_evidence/l1_reply_consistency_20261106.md`（**有条件批准**：仅限「货号及带单位数值的字面支持检查」，
  不构成解决税费幻觉的完整方案）
- 迁移：`0017_answer_consistency_audit.sql`（审计加 6 列）

## 1. 做了什么

新链路（RAG 段，`ANSWERED` 之前）：

```
生成 → 引用清单解析 → 白名单校验 → decide()
     → 【一致性检查：答案里的货号/带单位数值是否字面出现在**被引用的证据块**里】
     → 有未支持 claim ⇒ HUMAN_REVIEW_REQUIRED + refusal_code=UNSUPPORTED_CLAIM（保留答案与引用）
```

- 规则版本 `literal-support-v1`；
- 只跟**有效引用的证据块**比，不回退到全部证据（裁决 2）；
- 区间 `14-70mm`、尺寸 `24×24mm` **整体**核验，不拆散（裁决 3）；
- 数字与货号都带边界：`14mm` 不被 `114mm` 支持、短货号不被长货号支持；
- 单位白名单固定（mm/cm/ml/kg/g/l/L/%/℃/元/只/箱），"第 3 页"这类不进检查；
- 目录表把单位写在表头时，区间/尺寸允许结构体匹配（`24×24`），单值（`39.00元`）不放宽；
- 无 claim 时**不适用**（`unsupported=null`），不记成 100%。

## 2. 审计（迁移 0017）

`rag_queries` 新增：`consistency_rule / codes_total / codes_supported / numbers_total /
numbers_supported / consistency_ms`，CHECK 保证 `supported ≤ total`、`NULL` 表示不适用。

## 3. 实测

**真实语料（qwen3:14b）**

| 问题 | 状态 | 一致性字段 | 检查耗时 |
|---|---|---|---|
| `A112420 的规格是什么？` | ANSWERED（走结构化通道） | 全 NULL（未做检查） | — |
| `冷凝管的标口范围是多少？` | REFUSED（模型未给引用） | 走既有拒答分支 | — |
| `有哪些规格的试管？` | ANSWERED | 0/0 与 0/0（答案无货号/数值） | 0 ms |

检查本身开销 <1ms（`consistency_ms=0`），远低于裁决给的 5ms 基准。

**端到端断言（4 项）**

1. 货号与价格都在引用证据里 → `ANSWERED`，`unsupported={codes:0,numbers:0}`；
2. 货号 `A999999` + 价格 `88.00元` 不在引用证据里 → `HUMAN_REVIEW_REQUIRED` +
   `UNSUPPORTED_CLAIM`，`unsupported={codes:1,numbers:1}`，**引用保留**；
3. 答案没有货号/数值 → `unsupported=null`（不适用）；
4. 审计行写入 `literal-support-v1` 与四个计数、耗时。

**单元断言（11 项）**：区间整体、`14` vs `114`、同数不同单位、尺寸结构变化、短货号被长货号包含、
全角归一、价格必须带单位、无 claim 不适用等。

## 4. 迁移演练

```
fresh apply → OK；重复 apply → 幂等；rollback（无数据）→ OK；re-apply → OK
共享库有 53 行一致性记录 → rollback 被拒绝：FKH_ROLLBACK_BLOCKED
```

## 5. 它**抓不到**什么（裁决明确写下的边界）

- `tax-04`「价格不含税」：没有货号也没有带单位数值，两类检查都不触发，仍会 `ANSWERED`；
- 无单位的 `24*24`（`spec-02` 的错答形态）不在提取范围；
- 语义层面的"支持关系"（某条证据是否真的支撑这句话）不做判断。

**所以：检查通过 ≠ 答案正确**，只表示"未发现所覆盖的字面异常"。要解决税费幻觉，需要另一条
确定性规则或证据前置门禁，本次未做。
