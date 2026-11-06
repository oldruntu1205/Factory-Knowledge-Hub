"""带引用问答：提示词、引用覆盖检查与编排。"""

from __future__ import annotations

from fkh.answer.prompt import build_prompt, extract_evidence_ids, uncovered_sentences
from fkh.answer.service import AnswerNotConfigured, AnswerOutcome, AnswerService

__all__ = [
    "AnswerNotConfigured",
    "AnswerOutcome",
    "AnswerService",
    "build_prompt",
    "extract_evidence_ids",
    "uncovered_sentences",
]
