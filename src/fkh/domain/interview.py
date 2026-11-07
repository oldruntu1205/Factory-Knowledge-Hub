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
        granted_by: str | None = None,
        receipt_uri: str | None = None,
    ) -> uuid.UUID:
        self._bind()
        if method not in {"ORAL_WITH_RECEIPT", "WRITTEN"}:
            raise InterviewError(f"授权形式非法：{method}")
        if granted_by is not None and granted_by != subject_id:
            # 代授权需要书面授权书，ADR-05 A3 未裁决前一律拒绝
            raise InterviewError("只允许受访者本人授权")
        consent_id = uuid.uuid4()
        self._conn.execute(
            """INSERT INTO interview_consents
                 (tenant_id, consent_id, subject_id, method, receipt_uri, retention_days, granted_by)
               VALUES (%s, %s, %s, %s, %s, %s, %s)""",
            (self._tenant, consent_id, subject_id, method, receipt_uri, retention_days, subject_id),
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

    def revoke_consent(self, consent_id: uuid.UUID, *, reason: str | None = None) -> dict[str, Any]:
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
            drafts = self._conn.execute(
                """UPDATE experience_drafts
                      SET status = 'WITHDRAWN'
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
            "reason": reason,
        }

    # ---- 访谈 --------------------------------------------------------------
    def create_interview(
        self, *, subject_id: str, consent_id: uuid.UUID, interviewer: str, language: str
    ) -> uuid.UUID:
        self._bind()
        consent = self.consent(consent_id)
        if consent is None:
            raise InterviewNotFound(str(consent_id))
        if consent.revoked_at is not None:
            raise InterviewError("授权已撤回，不能建立访谈")
        if consent.subject_id != subject_id:
            raise InterviewError("授权主体与受访者不一致")
        interview_id = uuid.uuid4()
        self._conn.execute(
            """INSERT INTO interviews
                 (tenant_id, interview_id, subject_id, consent_id, interviewer, language, status)
               VALUES (%s, %s, %s, %s, %s, %s, 'CONSENTED')""",
            (self._tenant, interview_id, subject_id, consent_id, interviewer, language),
        )
        return interview_id

    def add_transcript(
        self, interview_id: uuid.UUID, *, content: str, kind: str = "MACHINE"
    ) -> uuid.UUID:
        self._bind()
        row = self._conn.execute(
            "SELECT status FROM interviews WHERE tenant_id = %s AND interview_id = %s",
            (self._tenant, interview_id),
        ).fetchone()
        if row is None:
            raise InterviewNotFound(str(interview_id))
        if str(row[0]) != "CONSENTED":
            raise InterviewError(f"当前状态 {row[0]} 不接受新转写")
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
    def build_draft(self, interview_id: uuid.UUID) -> uuid.UUID:
        """把转写切成"带时间戳的证据位 + 结论"，生成待人工审核的草稿。

        无 ASR：转写文本按 `[mm:ss]` 行解析；没有时间戳的行按顺序累积到上一段。
        这不是"AI 总结"，只是结构化，避免在 ADR-05 未签时引入模型判断。
        """
        self._bind()
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

    # ---- 查询 --------------------------------------------------------------
    def view(self, interview_id: uuid.UUID) -> dict[str, Any]:
        self._bind()
        row = self._conn.execute(
            """SELECT interview_id, subject_id, status, interviewer, language, created_at
                 FROM interviews WHERE tenant_id = %s AND interview_id = %s""",
            (self._tenant, interview_id),
        ).fetchone()
        if row is None:
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
