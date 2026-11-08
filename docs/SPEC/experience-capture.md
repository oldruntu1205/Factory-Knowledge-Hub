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
| `POST /v1/interviews/consents/{id}/revoke` | `interview.create` + 本人 | 删转写 + 清空草稿内容载荷 + 访谈 WITHDRAWN；非本人 403、重复撤回 409 |
| `POST /v1/interviews` | `interview.create` | **只能由授权本人建立**（采集者持 scope 也不能用他人授权）；主体不一致 / 已撤回 → 409；授权不存在 → 403 |
| `POST /v1/interviews/{id}/transcript` | `interview.create` | 只写 RAW 表 |
| `POST /v1/interviews/{id}/drafts` | `interview.create` | 只做结构化（不做模型总结） |
| `POST /v1/interviews/drafts/{id}/review` | `interview.review` | 审核人取自令牌 `sub`；AI 不得自我批准 |
| `POST /v1/interviews/drafts/{id}/promote` | `knowledge.write` | 只有 `APPROVED` 可晋升；产出 **DRAFT 版本**，仍需技术审核/公开审批；记录 `promoted_version_id` |
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

## 6. 草稿晋升与撤回联动（`0028`、2026-11-08）

- **晋升**：`POST /v1/interviews/drafts/{id}/promote` → 生成知识 `DRAFT` 版本并写入
  `experience_drafts.promoted_version_id`；调用者必须有 `knowledge.write`，
  且 `required_scope` 在其授权范围内。晋升**不会**直接可检索——仍需走审核/发布闸门。
- **撤回联动**：撤回授权时，已晋升版本按状态机走合法路径：
  `DRAFT`/`REVIEWED` → `discard`（DISCARDED）；`APPROVED_INTERNAL`/`PUBLISHED_PUBLIC` → `revoke`（REVOKED）。
  两者都会写 outbox，`DISCARDED`/`REVOKED` 也已被 `0019` 的课程联动触发器覆盖。
  实测：直接 `UPDATE ... SET status='REVOKED'` 会被 `fkh_version_transition` 触发器拒绝
  （`DRAFT → REVOKED` 非法），所以必须走领域方法。
- 验收：晋升前 409；晋升后检索不到（DRAFT）；撤回后 `versions_revoked` 含该版本且问答不再命中。

## 8. 撤回的权限与并发（2026-11-08 修复）

基线 `1b1701d` 的问题：`revoke_consent` 不校验调用者是否为授权本人；重复撤回静默通过；
撤回与转写/审核/晋升之间没有行锁。

| 规则 | 实现与验收 |
|---|---|
| 只有授权本人能撤回 | 先 `FOR UPDATE` 锁授权行；`subject_id != actor_subject` → **403**（与不存在同码，不探测） |
| 尚未建访谈时可撤回 | **200**，`interviews/transcripts_deleted/drafts_withdrawn` 全 0 |
| 重复撤回 | 第二次 **409** |
| 撤回 vs 转写/审核/晋升 | 写入方对授权行取 **FOR SHARE**：撤回未提交时写入阻塞；撤回提交后读到已撤回 → 409，且表内无残留行 |
| 已晋升内容 | 撤回时按状态机 `discard`（DRAFT/REVIEWED）/ `revoke`（APPROVED_INTERNAL/PUBLISHED_PUBLIC），检索立即为空 |

验收：`tests/e2e/test_interview_revoke.py`（5 条，真实 PG + 业务角色 + HTTP，含两条连接的真实并发用例）。
期间还修掉一个真实缺陷：`revoked_versions` 在"没有访谈"的分支里未初始化，导致 500。
