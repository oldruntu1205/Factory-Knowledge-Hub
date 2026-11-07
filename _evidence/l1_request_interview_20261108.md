# L1 审阅：经验采集最小可信版（`f30a002`，ADR-05 未签）

- 日期：2026-11-08
- 风险归类：**新 schema**、**权限（授权台账）**、**新对外契约**、**个人信息处理**
- 请只做判断：**不要探索仓库、不要读源码、不要跑测试**。

## 1. 本片不做什么（明确边界）

ADR-05 全部未签，因此：**不接真实 ASR、不落真实录音、不做模型总结、不做自动质量评分**。
转写文本由调用方（采集者）提交，本片只保证"没有授权不准采集、转写不泄漏、草稿必须人工审"。

## 2. 四张表（迁移 `0026`，修正在 `0027`）

| 表 | 关键约束 |
|---|---|
| `interview_consents` | `granted_by = subject_id`（**禁止代授权**，A3 默认最保守）；`retention_days` 1–3650；`revoked_at` |
| `interviews` | 必须引用有效授权；状态 `CONSENTED/TRANSCRIBED/DRAFT_READY/IN_REVIEW/APPROVED/REJECTED/WITHDRAWN` |
| `interview_transcripts` | **RAW**：`kind ∈ {MACHINE, HUMAN_EDITED}`，**不写入 `knowledge_chunks`** |
| `experience_drafts` | `evidence_spans jsonb`（`{start_ms,end_ms,quote}`）、`reviewer/reviewed_at/review_note` |

`0027` 修正：`experience_drafts.transcript_id` 由 `CASCADE` 改为可空 + `ON DELETE SET NULL (transcript_id)`，
并把撤回顺序改成"先作废草稿、再删转写"——否则删转写会把草稿行一起级联删除，
撤回后无法回答"当时有哪些草稿"（实测发现）。

## 3. 端点（替换两个 501 占位，契约已同步声明）

| 端点 | scope |
|---|---|
| `POST /v1/interviews/consents` | `interview.create` |
| `POST /v1/interviews/consents/{id}/revoke` | `interview.create` |
| `POST /v1/interviews` | `interview.create` |
| `POST /v1/interviews/{id}/transcript` | `interview.create` |
| `POST /v1/interviews/{id}/drafts` | `interview.create` |
| `POST /v1/interviews/drafts/{id}/review` | `interview.review` |
| `GET /v1/interviews/{id}` | `interview.read` |

错误口径：授权/访谈不存在 → **403**（与无权限同码，不探测存在性）；
状态不符/主体不一致/已撤回 → **409**。

## 4. 已验收（真实 PG + 受限身份 + HTTP，5 条）

1. 授权主体与受访者不一致 → 409；授权不存在 → 403；
2. **转写原句拿去检索 → 结果为空；拿去问答 → 非 `ANSWERED`**（转写不进索引）；
3. 草稿 `evidence_spans[0].start_ms == 12000`（`[00:12]`），每条带 `quote`；
   采集者无 `interview.review` → 自批准 403；审核人批准 → `APPROVED` 且记录 `reviewer`；
4. 撤回授权 → `transcripts_deleted=1`、`drafts_withdrawn=1`、访谈 `WITHDRAWN`、
   转写计数 0、草稿 `WITHDRAWN`（保留行与证据位）、该授权不能再建访谈（409）；
5. 不存在的访谈 id → 403。

门禁：`uv run pytest -q` → **606 passed**；ruff/mypy 全绿。

## 5. 需要你判断的四点

1. **"只允许本人授权"**（A3 最保守默认）是否可接受为 ADR-05 裁决前的实现？
   若要支持班组长代授权，需要授权书引用字段与额外审计，我下一片做。
2. **转写文本由采集者提交**（而非 ASR 产出）是否可接受？我理解这满足"不接真实 ASR"的约束，
   但需确认这不是变相绕过 B1（外部 ASR）。当前没有任何网络调用。
3. **草稿不做模型总结**：只用 `[mm:ss]` 结构化 + 关键词抽取（条件/例外/风险）。
   是否要求草稿阶段就引入模型摘要（那会触发 ADR-04/05 的模型路由问题）？
4. **撤回后草稿保留为 `WITHDRAWN`**（行与证据位保留，正文 `claim` 仍在库里）是否可接受？
   若要求"撤回即连草稿正文一起清除"，我可以把它改成软删除 + 正文置空，但会牺牲审计可读性。

## 6. 明确未做

- 真实录音上传与 ASR（ADR-05 B1/B2）；
- 保留期到期清理作业（B2）；
- 草稿 → `POST /v1/knowledge/drafts` 的晋升接线（C 组未决）；
- 第三方在场单独同意（A4）。
