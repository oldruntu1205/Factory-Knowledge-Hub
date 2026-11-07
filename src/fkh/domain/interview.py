"""经验采集（语音访谈）最小可信服务：授权台账 → RAW 转写 → 带时间戳草稿 → 人工审核 → 撤回。

不做的事（ADR-05 未签）：
- 不接真实 ASR（转写文本由调用方提供）；
- 不做自动质量评分/自动合并；
- 不把转写写进 `knowledge_chunks`（因此任何检索/问答路径都看不到）。

已落实的硬约束：
1. 没有**有效授权**（未撤销、授予人＝受访者本人）就不允许建访谈；
2. 转写只存 `interview_transcripts`（RAW），检索侧看不到；
3. 草稿必须带时间戳证据位（claim 与 quote 可追溯）；
4. 撤回授权：删除转写、作废草稿、访谈转 `WITHDRAWN`，审计留痕。
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

_STAMP = re.compile(r"^\s*\[(?P<mm>\d{1,3}):(?P<ss>\d{2})\]\s*(?P<text>.+)$")


class InterviewError(ValueError):
    """业务规则拒绝（状态不对、授权无效等）。"""


class InterviewNotFound(LookupError):
    """访谈/草稿不存在或不属于本租户（对外统一 403）。"""


@dataclass(frozen=True, slots=True)
class ConsentView:
    consent_id: uuid.UUID
    subject_id: str
    method: str
    retention_days: int
    revoked_at: Any


class InterviewService:
    def __init__(self, conn: psycopg.Connection[Any], tenant_id: uuid.UUID) -> None:
        self._conn = conn
        self._tenant = tenant_id
        self._bind()

    def _bind(self) -> None:
        self._conn.execute("SELECT set_config('app.tenant_id', %s, true)", (str(self._tenant),))

    # ---- 授权 --------------------------------------------------------------
    def grant_consent(
        self,
        *,
        subject_id: str,
        method: str,
        retention_days: int,
        authenticated_subject: str,
        receipt_uri: str | None = None,
    ) -> uuid.UUID:
        """登记授权。

        L1 2026-11-08：`granted_by` 必须由**服务端绑定当前认证主体**，
        不能只校验调用方提交的 `granted_by == subject_id`（那等于让改写请求体自证）。
        因此这里要求 `authenticated_subject == subject_id`：只有本人账号能授权；
        班组长代授权（ADR-05 A3）未裁决，一律拒绝。
        """
        self._bind()
        if method not in {"ORAL_WITH_RECEIPT", "WRITTEN"}:
            raise InterviewError(f"授权形式非法：{method}")
        if authenticated_subject != subject_id:
            raise InterviewError("只允许受访者本人授权（需本人账号）")
        consent_id = uuid.uuid4()
        self._conn.execute(
            """INSERT INTO interview_consents
                 (tenant_id, consent_id, subject_id, method, receipt_uri, retention_days, granted_by)
               VALUES (%s, %s, %s, %s, %s, %s, %s)""",
            (self._tenant, consent_id, subject_id, method, receipt_uri, retention_days,
             authenticated_subject),
        )
        return consent_id

    def consent(self, consent_id: uuid.UUID) -> ConsentView | None:
        self._bind()
        row = self._conn.execute(
            """SELECT consent_id, subject_id, method, retention_days, revoked_at
                 FROM interview_consents WHERE tenant_id = %s AND consent_id = %s""",
            (self._tenant, consent_id),
        ).fetchone()
        if row is None:
            return None
        return ConsentView(
            consent_id=uuid.UUID(str(row[0])),
            subject_id=str(row[1]),
            method=str(row[2]),
            retention_days=int(row[3]),
            revoked_at=row[4],
        )

    def revoke_consent(
        self,
        consent_id: uuid.UUID,
        *,
        actor_id: uuid.UUID,
        actor_subject: str,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """撤回授权：删除该授权下所有转写，作废未发布草稿，访谈转 WITHDRAWN。"""
        self._bind()
        consent = self.consent(consent_id)
        if consent is None:
            raise InterviewNotFound(str(consent_id))
        self._conn.execute(
            "UPDATE interview_consents SET revoked_at = now()"
            " WHERE tenant_id = %s AND consent_id = %s AND revoked_at IS NULL",
            (self._tenant, consent_id),
        )
        interviews = self._conn.execute(
            "SELECT interview_id FROM interviews WHERE tenant_id = %s AND consent_id = %s",
            (self._tenant, consent_id),
        ).fetchall()
        interview_ids = [uuid.UUID(str(row[0])) for row in interviews]
        removed_transcripts = 0
        withdrawn_drafts = 0
        if interview_ids:
            # 先作废草稿（保留行与证据位供审计），再删转写；
            # FK 为 ON DELETE SET NULL (transcript_id)，草稿行不会被级联带走。
            # L1 2026-11-08：撤回后保留审计行，但**清除内容载荷**（claim/条件/例外/风险/引文）；
            # 时间位置可保留（不含原文），避免"撤回=数据仍可读"。
            drafts = self._conn.execute(
                """UPDATE experience_drafts
                      SET status = 'WITHDRAWN',
                          claim = '',
                          conditions = NULL,
                          exceptions = NULL,
                          risks = NULL,
                          evidence_spans = (
                            SELECT coalesce(jsonb_agg(
                                     jsonb_build_object('start_ms', span->'start_ms',
                                                        'end_ms', span->'end_ms')), '[]'::jsonb)
                              FROM jsonb_array_elements(evidence_spans) AS span
                          )
                    WHERE tenant_id = %s AND interview_id = ANY(%s)
                      AND status <> 'WITHDRAWN'""",
                (self._tenant, interview_ids),
            )
            withdrawn_drafts = int(drafts.rowcount)
            removed = self._conn.execute(
                "DELETE FROM interview_transcripts WHERE tenant_id = %s AND interview_id = ANY(%s)",
                (self._tenant, interview_ids),
            )
            removed_transcripts = int(removed.rowcount)

            # 已晋升的知识：授权撤回后必须撤下（不能静默保留），并写 outbox 供异步清理
            promoted = self._conn.execute(
                """SELECT DISTINCT d.promoted_version_id
                     FROM experience_drafts d
                    WHERE d.tenant_id = %s AND d.interview_id = ANY(%s)
                      AND d.promoted_version_id IS NOT NULL""",
                (self._tenant, interview_ids),
            ).fetchall()
            revoked_versions: list[str] = []
            if promoted:
                # 走状态机认可的路径：未发布的走 discard（DRAFT/REVIEWED → DISCARDED），
                # 已批准的走 revoke（APPROVED_INTERNAL/PUBLISHED_PUBLIC → REVOKED）。
                # 直接 UPDATE 会被 fkh_version_transition 触发器拒绝（实测 DRAFT→REVOKED 非法）。
                from fkh.domain.review import ReviewService

                review = ReviewService(self._conn, self._tenant)
                for row in promoted:
                    version_id = uuid.UUID(str(row[0]))
                    status_row = self._conn.execute(
                        "SELECT status FROM knowledge_versions WHERE tenant_id = %s AND id = %s",
                        (self._tenant, version_id),
                    ).fetchone()
                    if status_row is None:
                        continue
                    status = str(status_row[0])
                    try:
                        if status in {"DRAFT", "REVIEWED"}:
                            review.discard(
                                version_id, actor_id, reason or "consent_withdrawn"
                            )
                            revoked_versions.append(str(version_id))
                        elif status in {"APPROVED_INTERNAL", "PUBLISHED_PUBLIC"}:
                            review.revoke(
                                version_id, actor_id, reason or "consent_withdrawn"
                            )
                            revoked_versions.append(str(version_id))
                    except Exception:  # noqa: BLE001 - 单条失败不阻断授权撤回本身
                        continue
            self._conn.execute(
                """UPDATE interviews SET status = 'WITHDRAWN', updated_at = now()
                    WHERE tenant_id = %s AND interview_id = ANY(%s)""",
                (self._tenant, interview_ids),
            )
        return {
            "consent_id": str(consent_id),
            "interviews": len(interview_ids),
            "transcripts_deleted": removed_transcripts,
            "drafts_withdrawn": withdrawn_drafts,
            "versions_revoked": revoked_versions,
            "reason": reason,
        }

    # ---- 访谈 --------------------------------------------------------------
    def create_interview(
        self, *, subject_id: str, consent_id: uuid.UUID, interviewer: str, language: str
    ) -> uuid.UUID:
        """建立访谈。

        L1 2026-11-08：持有 `interview.create` **不等于**可以使用任意人的授权——
        访谈主体必须与授权主体一致，且授权仍然有效。
        """
        self._bind()
        consent = self.consent(consent_id)
        if consent is None:
            raise InterviewNotFound(str(consent_id))
        if consent.revoked_at is not None:
            raise InterviewError("授权已撤回，不能建立访谈")
        if consent.subject_id != subject_id:
            raise InterviewError("授权主体与受访者不一致")
        if interviewer != subject_id:
            # L1 2026-11-08：持有 interview.create 不等于可以使用任意人的授权。
            # 在 ADR-05 A3（代授权）裁决前，只允许授权本人在自己的会话里建立访谈。
            raise InterviewError("只能由授权本人建立访谈")
        interview_id = uuid.uuid4()
        self._conn.execute(
            """INSERT INTO interviews
                 (tenant_id, interview_id, subject_id, consent_id, interviewer, language, status)
               VALUES (%s, %s, %s, %s, %s, %s, 'CONSENTED')""",
            (self._tenant, interview_id, subject_id, consent_id, interviewer, language),
        )
        return interview_id

    def add_transcript(
        self, interview_id: uuid.UUID, *, content: str, actor: str, kind: str = "MACHINE"
    ) -> uuid.UUID:
        self._bind()
        row = self._conn.execute(
            "SELECT status, interviewer FROM interviews"
            " WHERE tenant_id = %s AND interview_id = %s",
            (self._tenant, interview_id),
        ).fetchone()
        if row is None:
            raise InterviewNotFound(str(interview_id))
        if str(row[1]) != actor:
            # 对象级权限：只有该访谈的建立者（=授权本人）能写入
            raise InterviewNotFound(str(interview_id))
        if str(row[0]) != "CONSENTED":
            raise InterviewError(f"当前状态 {row[0]} 不接受新转写")
        if self._consent_revoked(interview_id):
            raise InterviewError("授权已撤回，不能登记转写")
        if not content.strip():
            raise InterviewError("转写内容为空")
        transcript_id = uuid.uuid4()
        self._conn.execute(
            """INSERT INTO interview_transcripts
                 (tenant_id, transcript_id, interview_id, kind, content)
               VALUES (%s, %s, %s, %s, %s)""",
            (self._tenant, transcript_id, interview_id, kind, content),
        )
        self._conn.execute(
            "UPDATE interviews SET status = 'TRANSCRIBED', updated_at = now()"
            " WHERE tenant_id = %s AND interview_id = %s",
            (self._tenant, interview_id),
        )
        return transcript_id

    # ---- 草稿 --------------------------------------------------------------
    def build_draft(self, interview_id: uuid.UUID, *, actor: str) -> uuid.UUID:
        """把转写切成"带时间戳的证据位 + 结论"，生成待人工审核的草稿。

        无 ASR：转写文本按 `[mm:ss]` 行解析；没有时间戳的行按顺序累积到上一段。
        这不是"AI 总结"，只是结构化，避免在 ADR-05 未签时引入模型判断。
        """
        self._bind()
        owner = self._conn.execute(
            "SELECT interviewer FROM interviews WHERE tenant_id = %s AND interview_id = %s",
            (self._tenant, interview_id),
        ).fetchone()
        if owner is None or str(owner[0]) != actor:
            raise InterviewNotFound(str(interview_id))
        if self._consent_revoked(interview_id):
            raise InterviewError("授权已撤回，不能生成草稿")
        row = self._conn.execute(
            """SELECT t.transcript_id, t.content FROM interview_transcripts t
                WHERE t.tenant_id = %s AND t.interview_id = %s
                ORDER BY t.created_at DESC LIMIT 1""",
            (self._tenant, interview_id),
        ).fetchone()
        if row is None:
            raise InterviewError("尚无转写，不能生成草稿")
        transcript_id = uuid.UUID(str(row[0]))
        segments = _segments(str(row[1]))
        if not segments:
            raise InterviewError("转写没有可用段落")

        claim = segments[0]["text"][:500]
        spans = [
            {"start_ms": seg["start_ms"], "end_ms": seg["end_ms"], "quote": seg["text"][:300]}
            for seg in segments
        ]
        open_questions = ["是否需要补充量化数据？"]
        draft_id = uuid.uuid4()
        self._conn.execute(
            """INSERT INTO experience_drafts
                 (tenant_id, draft_id, interview_id, transcript_id, status, claim,
                  conditions, exceptions, risks, evidence_spans, open_questions)
               VALUES (%s, %s, %s, %s, 'DRAFT', %s, %s, %s, %s, %s, %s)""",
            (
                self._tenant,
                draft_id,
                interview_id,
                transcript_id,
                claim,
                _find_keyword(segments, "条件"),
                _find_keyword(segments, "例外"),
                _find_keyword(segments, "风险"),
                Jsonb(spans),
                Jsonb(open_questions),
            ),
        )
        self._conn.execute(
            "UPDATE interviews SET status = 'DRAFT_READY', updated_at = now()"
            " WHERE tenant_id = %s AND interview_id = %s",
            (self._tenant, interview_id),
        )
        return draft_id

    def review_draft(
        self, draft_id: uuid.UUID, *, reviewer: str, decision: str, note: str | None = None
    ) -> dict[str, Any]:
        """人工审核：APPROVE/REJECT。AI 不能自我批准（评审人必须来自令牌）。"""
        self._bind()
        if decision not in {"APPROVE", "REJECT"}:
            raise InterviewError(f"decision 非法：{decision}")
        revoked = self._conn.execute(
            """SELECT 1 FROM experience_drafts d
                 JOIN interviews i ON i.tenant_id = d.tenant_id AND i.interview_id = d.interview_id
                 JOIN interview_consents c ON c.tenant_id = i.tenant_id AND c.consent_id = i.consent_id
                WHERE d.tenant_id = %s AND d.draft_id = %s AND c.revoked_at IS NOT NULL""",
            (self._tenant, draft_id),
        ).fetchone()
        if revoked is not None:
            raise InterviewError("授权已撤回，不能审核")
        row = self._conn.execute(
            """SELECT interview_id, status FROM experience_drafts
                WHERE tenant_id = %s AND draft_id = %s""",
            (self._tenant, draft_id),
        ).fetchone()
        if row is None:
            raise InterviewNotFound(str(draft_id))
        if str(row[1]) not in {"DRAFT", "IN_REVIEW"}:
            raise InterviewError(f"草稿状态 {row[1]} 不可审核")
        status = "APPROVED" if decision == "APPROVE" else "REJECTED"
        self._conn.execute(
            """UPDATE experience_drafts
                  SET status = %s, reviewer = %s, reviewed_at = now(), review_note = %s
                WHERE tenant_id = %s AND draft_id = %s""",
            (status, reviewer, note, self._tenant, draft_id),
        )
        self._conn.execute(
            "UPDATE interviews SET status = %s, updated_at = now()"
            " WHERE tenant_id = %s AND interview_id = %s",
            (status, self._tenant, row[0]),
        )
        return {"draft_id": str(draft_id), "status": status, "reviewer": reviewer}

    def promote_draft(
        self,
        draft_id: uuid.UUID,
        *,
        actor_id: uuid.UUID,
        actor_subject: str,
        domain: str,
        required_scope: str,
        classification: int = 1,
    ) -> dict[str, Any]:
        """把已批准的经验草稿晋升为知识草稿版本。

        约束（Rev2 §3.3）：
        - 只有 `APPROVED` 且未撤回的草稿可晋升（AI 不能跳过人工审核）；
        - 晋升只到 `DRAFT` 状态，后续仍需走技术审核与（如需对外）公开审批；
        - 记录 `promoted_version_id`，使撤回授权时可连带撤销已晋升知识。
        """
        from fkh.authz.policy import AclPolicy
        from fkh.domain.state_machine import VersionStatus
        from fkh.workers.ingestion import IngestionRequest, ingest_document

        if domain not in {"owner_experience", "quality", "training"}:
            raise InterviewError(f"domain 非法：{domain}")
        self._bind()
        row = self._conn.execute(
            """SELECT d.status, i.status, d.claim, d.conditions, d.exceptions, d.risks,
                      d.interview_id, i.language
                 FROM experience_drafts d
                 JOIN interviews i ON i.tenant_id = d.tenant_id AND i.interview_id = d.interview_id
                WHERE d.tenant_id = %s AND d.draft_id = %s""",
            (self._tenant, draft_id),
        ).fetchone()
        if row is None:
            raise InterviewNotFound(str(draft_id))
        draft_status, interview_status = str(row[0]), str(row[1])
        if draft_status != "APPROVED":
            raise InterviewError(f"只有已批准的草稿可晋升，当前为 {draft_status}")
        if interview_status == "WITHDRAWN":
            raise InterviewError("访谈已撤回授权，不能晋升")
        if self._consent_revoked(uuid.UUID(str(row[6]))):
            raise InterviewError("授权已撤回，不能晋升")

        body_parts = [f"采访记录（{actor_subject} 录入）", "", f"结论：{row[2]}"]
        for label, value in (("条件", row[3]), ("例外", row[4]), ("风险", row[5])):
            if value:
                body_parts.extend(["", f"{label}：{value}"])
        body_parts.extend(["", "说明：本条经验来自人工审核通过的访谈草稿，仍需技术审核后使用。"])
        content = ("\n".join(body_parts) + "\n").encode()

        acl = AclPolicy.from_json({"allow": {"roles": ["staff"]}, "deny": {}})
        result = ingest_document(
            self._conn,
            self._tenant,
            IngestionRequest(
                source_id=f"draft-{draft_id}",
                filename=f"experience-{draft_id}.md",
                content=content,
                knowledge_key=f"experience-{draft_id}",
                title=f"经验记录：{str(row[2])[:40]}",
                domain=domain,
                owner_department="production",
                classification=classification,
                audience="internal",
                required_scope=required_scope,
                acl_policy=acl,
                actor_id=actor_id,
                language=str(row[7]),
            ),
        )
        assert result.status is VersionStatus.DRAFT
        self._conn.execute(
            """UPDATE experience_drafts
                  SET promoted_version_id = %s, promoted_at = now()
                WHERE tenant_id = %s AND draft_id = %s""",
            (result.version_id, self._tenant, draft_id),
        )
        return {
            "draft_id": str(draft_id),
            "document_id": str(result.document_id),
            "version_id": str(result.version_id),
            "status": result.status.value,
        }

    # ---- 查询 --------------------------------------------------------------
    def view(
        self, interview_id: uuid.UUID, *, viewer: str, allow_reviewer: bool = False
    ) -> dict[str, Any]:
        self._bind()
        row = self._conn.execute(
            """SELECT interview_id, subject_id, status, interviewer, language, created_at
                 FROM interviews WHERE tenant_id = %s AND interview_id = %s""",
            (self._tenant, interview_id),
        ).fetchone()
        if row is None:
            raise InterviewNotFound(str(interview_id))
        if not allow_reviewer and str(row[1]) != viewer:
            # 对象级权限：非本人需要审核权限才能读取
            raise InterviewNotFound(str(interview_id))
        drafts = self._conn.execute(
            """SELECT draft_id, status, claim, evidence_spans
                 FROM experience_drafts WHERE tenant_id = %s AND interview_id = %s
                ORDER BY created_at""",
            (self._tenant, interview_id),
        ).fetchall()
        transcript_count = self._conn.execute(
            "SELECT count(*) FROM interview_transcripts WHERE tenant_id = %s AND interview_id = %s",
            (self._tenant, interview_id),
        ).fetchone()
        return {
            "interview_id": str(row[0]),
            "subject_id": str(row[1]),
            "status": str(row[2]),
            "interviewer": str(row[3]),
            "language": str(row[4]),
            "created_at": row[5].isoformat() if row[5] else None,
            "transcripts": int(transcript_count[0]) if transcript_count else 0,
            "drafts": [
                {
                    "draft_id": str(d[0]),
                    "status": str(d[1]),
                    "claim": str(d[2]),
                    "evidence_spans": d[3],
                }
                for d in drafts
            ],
        }


    def _consent_revoked(self, interview_id: uuid.UUID) -> bool:
        row = self._conn.execute(
            """SELECT c.revoked_at FROM interviews i
                 JOIN interview_consents c
                   ON c.tenant_id = i.tenant_id AND c.consent_id = i.consent_id
                WHERE i.tenant_id = %s AND i.interview_id = %s""",
            (self._tenant, interview_id),
        ).fetchone()
        return row is not None and row[0] is not None


def _segments(content: str) -> list[dict[str, Any]]:
    segments: list[dict[str, Any]] = []
    for line in content.splitlines():
        match = _STAMP.match(line)
        if match:
            minutes = int(match.group("mm"))
            seconds = int(match.group("ss"))
            start_ms = (minutes * 60 + seconds) * 1000
            segments.append({"start_ms": start_ms, "end_ms": start_ms + 15_000, "text": match.group("text").strip()})
        elif line.strip() and segments:
            segments[-1]["text"] += " " + line.strip()
        elif line.strip():
            segments.append({"start_ms": 0, "end_ms": 15_000, "text": line.strip()})
    return [seg for seg in segments if seg["text"]]


def _find_keyword(segments: list[dict[str, Any]], keyword: str) -> str | None:
    for seg in segments:
        text = str(seg["text"])
        if keyword in text:
            return text[:300]
    return None


__all__ = ["ConsentView", "InterviewError", "InterviewNotFound", "InterviewService"]
