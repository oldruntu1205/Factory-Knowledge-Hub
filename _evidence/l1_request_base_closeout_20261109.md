# L1 送审：RAG 基座 V1 收尾（契约清理 / 实例配置参数化 / 承诺词追加）

- 日期：2026-11-09
- 基线：`767df24`（统计刷新自动化已 PASS）
- 沙箱：`read-only`；不要探索仓库，只基于本文判断。
- 预算：本切片第 1 次 L1 调用。

## 一、改动

### 1) V1 契约移除两个 501 端点

- 删除 `POST /v1/sources`、`POST /v1/knowledge/drafts` 的**路由注册**与 OpenAPI 声明；
  现在返回 404，而不是永久 501。
- 理由：两者都是“对象存储上传凭据/空白草稿”的后续形态，V1 已有真实入口：
  - 导入：`POST /v1/knowledge/documents`；
  - 草稿：上传流程/`POST /v1/interviews/drafts/{id}/promote`。
- 同步删除 OpenAPI 的 `NotImplemented` 组件，更新契约说明；`ErrorCode.NOT_IMPLEMENTED`
  映射保留（未使用，供后续版本）。
- 契约测试 `test_openapi_matches_registered_routes` 双向校验通过。

### 2) 实例首装参数化

`instance provision` 新增：
```text
--domain CODE=LABEL        可重复；不写则用通用四域模板
--catalog-pattern REGEX    默认 ^[A-Z]{2}-[0-9]{4}$
--currencies USD,EUR       默认 CNY
```
- 域 code 校验 `[a-z][a-z0-9_]*`、去重；币种必须 3 位大写；正则编译失败即拒绝。
- `record` 增 `catalog_pattern`/`currencies`；域/规则写入数据库，后续仍可由
  `POST /v1/config/domains`、`PUT /v1/config/catalog-rules` 修改。

### 3) 商务承诺词表按实例追加

- 新增 `FKH_COMMITMENT_KEYWORDS_EXTRA`（逗号分隔）；
- `build_commitment_keywords()` 返回“内置安全词 + 追加词”，**不提供删除内置词的入口**；
- `AnswerService` 接收词表；`main._answer` 与 `answer_exec` 按 Settings 传入；
- 仅对 `customer_public` 外部客服生效；追加词命中即转人工。

### 4) 文档/收尾包

- 新增 `docs/BASE-CLOSEOUT.md`：能力边界、配置入口、验收命令、客户验收包、残留、回滚；
- 更新 `RELEASE-READINESS`（0001–0037、739 passed、release_check GO/WARN）、
  `HANDOFF`（快照/迁移台账）、`DEPLOY-SINGLE-INSTANCE`、ADR-009；
- `experience-capture` 的“草稿晋升未接线”旧说法已改为实际 promote 路由。

## 二、证据

- `pytest -q` → **739 passed**；ruff/mypy 全绿。
- 测试库 `release_check` → **12 PASS / 1 WARN / 0 FAIL，GO**（WARN=provider none）。
- 新增 9 条单测：承诺词追加/仍保留内置词、域替换/非法拒绝、币种大写/非法拒绝、provision 参数解析。
- 现有契约/答案/客户可见性/黄金集测试全过（32 passed 定向）。

## 三、请 L1 判断

1. 从 V1 契约移除 `POST /v1/sources`、`POST /v1/knowledge/drafts`（原 501）是否可接受？
   还是必须保留 501 作为兼容占位/实现别名？
2. `provision` 的域/货号正则/币种参数化与校验，是否足以支撑“一客户一实例、不改内核”的基座目标？
3. 承诺词表“只追加、不删除内置词”的安全取舍是否可接受？是否要求完整替换能力？
4. 本切片是否可 PASS，作为 RAG 基座 V1 收尾？

## 四、固定回复骨架

```text
VERDICT: PASS | CHANGES_REQUESTED

MUST_FIX:
- <问题>: <为什么阻塞> -> <最小修复>

SHOULD_FIX:
- <问题>: <建议>

BOUNDARY:
- <本轮不要求覆盖的项>
```
