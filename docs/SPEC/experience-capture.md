# 经验采集（语音访谈）最小可信实现（Rev2 §3.3，ADR-05 未签）

> 2026-11-08：迁移 `0026`/`0027`，端点 `/v1/interviews/*`，验收 `tests/e2e/test_interview_experience.py`（5 条）。

## 1. 链路

```
授权台账（本人授权） → 建访谈 → 登记转写（RAW） → 生成带时间戳草稿 → 人工审核 → （后续）成为知识草稿
                                        ↘ 撤回授权：删转写、作废草稿、访谈 WITHDRAWN
```

## 2. 四张表

| 表 | 作用 | 关键约束 |
|---|---|---|
| `interview_consents` | 授权台账 | `granted_by` 由**服务端绑定当前认证主体**，且必须等于受访者（禁止代授权）；保留期 1–3650 天；`revoked_at` |
| `interviews` | 访谈任务 | 必须引用有效授权；状态机 `CONSENTED → TRANSCRIBED → DRAFT_READY → IN_REVIEW/APPROVED/REJECTED/WITHDRAWN` |
| `interview_transcripts` | **RAW 转写** | **不进 `knowledge_chunks`**，所以检索/问答看不到 |
| `experience_drafts` | 带时间戳草稿 | `evidence_spans` 存 `{start_ms,end_ms,quote}`；`reviewer/reviewed_at/review_note` |

## 3. 端点与权限

| 端点 | scope | 说明 |
|---|---|---|
| `POST /v1/interviews/consents` | `interview.create` | 只允许本人授权 |
| `POST /v1/interviews/consents/{id}/revoke` | `interview.create` | 删转写 + 作废草稿 + 访谈 WITHDRAWN |
| `POST /v1/interviews` | `interview.create` | **只能由授权本人建立**（采集者持 scope 也不能用他人授权）；主体不一致 / 已撤回 → 409；授权不存在 → 403 |
| `POST /v1/interviews/{id}/transcript` | `interview.create` | 只写 RAW 表 |
| `POST /v1/interviews/{id}/drafts` | `interview.create` | 只做结构化（不做模型总结） |
| `POST /v1/interviews/drafts/{id}/review` | `interview.review` | 审核人取自令牌 `sub`；AI 不得自我批准 |
| `GET /v1/interviews/{id}` | `interview.read` | 状态 + 转写计数 + 草稿与证据位 |

## 4. 验收要点

- 无授权不能建访谈；主体不一致 409；授权不存在 403；
- 用转写原句去检索 → 结果为空；去问答 → 非 ANSWERED（**转写不进索引**）；
- 草稿 `evidence_spans[0].start_ms == 12000`（`[00:12]` 解析），每条带 `quote`；
- 采集者没有 `interview.review` → 自批准 403；审核人批准后 `APPROVED` 且记录 `reviewer`；
- 撤回授权：`transcripts_deleted=1`、`drafts_withdrawn=1`、访谈 `WITHDRAWN`、转写计数 0、
  草稿状态 `WITHDRAWN` 且 **`claim` 置空、`evidence_spans` 去掉 `quote` 只留时间位置**、
  该授权不能再建访谈（409）、撤回后转写/草稿/审核一律拒绝；
- 采集者持 `interview.create` 但用他人授权建访谈 → 409（对象级权限）；
- 非本人且无 `interview.review` 读取访谈 → 403。

## 5. 明确未做（等 ADR-05 / 后续片）

1. **真实录音上传与 ASR**：ADR-05 B1/B2 未签，转写文本由调用方提供；
2. **草稿 → 知识草稿的晋升**：需要在审核通过后走既有 `POST /v1/knowledge/drafts` 流程，
   尚未接线（避免与 ADR-05 C 组未决项冲突）；
3. **保留期只是配置、不是保证**：`retention_days` 已落库，但到期清理作业未实现
   （ADR-05 B2）——**不得对外宣称保留期已落实**；
4. 第三方在场（客户/供应商声音）的单独同意：表结构可承载（一次访谈一份授权），
   校验逻辑待裁决（ADR-05 A4）。
