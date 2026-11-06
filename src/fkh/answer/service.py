"""带引用问答服务（`POST /v1/knowledge/answer`）。

链路：检索（含检索审计）→ 上下文装配 → 本地推理 → 引用白名单核验 → 引用覆盖检查 →
状态判定 → 问答审计（`rag_queries` + `rag_citations`）。

三条硬边界（2026-11-06 L1 裁决）：
1. 鉴权与请求校验在端点完成；**配置缺失返回 503 并记 `ERROR` 审计**，越权不得被配置错误掩盖；
2. 推理失败是**能力故障**（503 + 审计 `ERROR`），不是 `REFUSED`——后者表示"知识不足"；
3. 审计与引用记录原子写入；写不进去就抛出去，绝不返回"成功"。

query 明文不落库、不进日志、不进异常：审计只存 HMAC 摘要（`rag_queries.query_hash`）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

from fkh.answer.prompt import build_prompt, extract_evidence_ids, uncovered_sentences
from fkh.generation import GenerationError, TextGenerator
from fkh.retrieval.citation import (
    AnswerPlan,
    AnswerStatus,
    Citation,
    RefusalCode,
    build_citations,
    decide,
    to_response,
)
from fkh.retrieval.context import ContextBlock, assemble_context
from fkh.retrieval.search import (
    LEXICAL_MODE,
    SearchContext,
    SearchService,
    query_fingerprint,
)


class AnswerNotConfigured(RuntimeError):
    """没有可用的本地推理能力（provider=none / 未配模型 / 模型不存在 / 缺审计密钥）。"""


@dataclass(frozen=True, slots=True)
class AnswerOutcome:
    payload: dict[str, Any]
    status: AnswerStatus
    model_id: str | None


class AnswerService:
    def __init__(
        self,
        conn: psycopg.Connection[Any],
        *,
        generator: TextGenerator | None,
        hash_key: str | None,
        hash_key_id: str,
        embedder: Any = None,
        rrf_k: int = 60,
        retrieval_mode: str = LEXICAL_MODE,
        max_tokens: int = 1024,
        temperature: float = 0.0,
        max_blocks: int = 8,
        max_context_tokens: int = 6000,
    ) -> None:
        self._conn = conn
        self._generator = generator
        self._hash_key = hash_key
        self._hash_key_id = hash_key_id
        self._embedder = embedder
        self._rrf_k = rrf_k
        self._retrieval_mode = retrieval_mode
        self._max_tokens = max_tokens
        self._temperature = temperature
        self._max_blocks = max_blocks
        self._max_context_tokens = max_context_tokens

    # ---- 主流程 ------------------------------------------------------------
    def answer(
        self,
        query: str,
        *,
        knowledge_scope: str,
        language: str,
        max_evidence: int,
        context: SearchContext,
    ) -> AnswerOutcome:
        trace_id = context.trace_id
        # 审计只能存摘要：先算哈希，算不出来（无密钥）就不具备审计条件
        query_hash = query_fingerprint(self._hash_key, query) if self._hash_key else None

        if self._generator is None:
            if query_hash is not None:
                self._audit(
                    context=context,
                    knowledge_scope=knowledge_scope,
                    status="ERROR",
                    refusal_code=None,
                    model_id=None,
                    citations=(),
                    trace_id=trace_id,
                    query_hash=query_hash,
                )
            raise AnswerNotConfigured("未配置本地推理模型（FKH_LLM_PROVIDER / FKH_LLM_MODEL）")

        result = SearchService(
            self._conn,
            hash_key=self._hash_key,
            hash_key_id=self._hash_key_id,
            embedder=self._embedder,
            rrf_k=self._rrf_k,
        ).search(
            query,
            knowledge_scope=knowledge_scope,
            max_results=max_evidence,
            context=context,
            retrieval_mode=self._retrieval_mode,
        )
        assembly = assemble_context(
            self._conn,
            context.tenant_id,
            result.hits,
            context.subject,
            max_blocks=self._max_blocks,
            max_tokens=self._max_context_tokens,
        )

        if not assembly.blocks:
            plan = decide((), build_citations((), (), knowledge_id_by_version={}), answer_text="")
            self._persist(
                context, knowledge_scope, plan, model_id=None, trace_id=trace_id, query_hash=query_hash
            )
            return AnswerOutcome(
                payload=to_response(plan, answer_text="", trace_id=trace_id, index_generation=0),
                status=plan.status,
                model_id=None,
            )

        prompt = build_prompt(query, assembly.blocks, language)
        try:
            generated = self._generator.generate(
                prompt, max_tokens=self._max_tokens, temperature=self._temperature
            )
        except GenerationError:
            self._audit(
                context=context,
                knowledge_scope=knowledge_scope,
                status="ERROR",
                refusal_code=None,
                model_id=self._generator.model_id,
                citations=(),
                trace_id=trace_id,
                query_hash=query_hash,
            )
            raise

        blocks = assembly.blocks
        knowledge_id_by_version = {str(block.version_id): str(block.document_id) for block in blocks}
        build = build_citations(
            extract_evidence_ids(generated.text),
            blocks,
            knowledge_id_by_version=knowledge_id_by_version,
        )
        plan = decide(blocks, build, answer_text=generated.text)

        if plan.status is AnswerStatus.ANSWERED:
            uncovered = uncovered_sentences(
                generated.text, [citation.evidence_id for citation in plan.citations]
            )
            if uncovered:
                # 合法 ID 只证明"引用存在"，不证明"结论被覆盖"：降级为拒答 + 人工复核
                plan = AnswerPlan(
                    status=AnswerStatus.REFUSED,
                    citations=plan.citations,
                    requires_human_review=True,
                    refusal_code=RefusalCode.INSUFFICIENT_EVIDENCE,
                )

        payload = to_response(
            plan,
            answer_text=generated.text,
            trace_id=trace_id,
            index_generation=self._index_generation(context.tenant_id, blocks),
        )
        self._persist(
            context, knowledge_scope, plan, model_id=generated.model_id, trace_id=trace_id, query_hash=query_hash
        )
        return AnswerOutcome(payload=payload, status=plan.status, model_id=generated.model_id)

    # ---- 内部 --------------------------------------------------------------
    def _index_generation(self, tenant_id: UUID, blocks: tuple[ContextBlock, ...]) -> int:
        versions = [block.version_id for block in blocks]
        row = self._conn.execute(
            """SELECT coalesce(max(index_generation), 0)
                 FROM knowledge_chunks
                WHERE tenant_id = %s AND version_id = ANY(%s)""",
            (tenant_id, versions),
        ).fetchone()
        return int(row[0]) if row is not None else 0

    def _persist(
        self,
        context: SearchContext,
        knowledge_scope: str,
        plan: AnswerPlan,
        *,
        model_id: str | None,
        trace_id: str,
        query_hash: str | None,
    ) -> None:
        self._audit(
            context=context,
            knowledge_scope=knowledge_scope,
            status=plan.status.value,
            refusal_code=plan.refusal_code.value if plan.refusal_code else None,
            model_id=model_id,
            citations=plan.citations,
            trace_id=trace_id,
            query_hash=query_hash,
        )

    def _audit(
        self,
        *,
        context: SearchContext,
        knowledge_scope: str,
        status: str,
        refusal_code: str | None,
        model_id: str | None,
        citations: tuple[Citation, ...],
        trace_id: str,
        query_hash: str | None,
    ) -> None:
        """写问答审计（主记录 + 引用），同一事务。

        失败必须抛出：调用方据此返回 5xx，而不是"报了成功但没记上"。
        """
        if query_hash is None:
            raise AnswerNotConfigured("未配置 FKH_RETRIEVAL_HASH_KEY，无法写合规审计")

        with self._conn.transaction():
            row = self._conn.execute(
                """INSERT INTO rag_queries
                     (tenant_id, principal_id, query_hash, scope, status, refusal_code,
                      trace_id, model_id)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                   RETURNING id""",
                (
                    context.tenant_id,
                    context.principal_id,
                    query_hash,
                    knowledge_scope,
                    status,
                    refusal_code,
                    trace_id,
                    model_id,
                ),
            ).fetchone()
            if row is None:
                raise RuntimeError("rag_queries 写入失败")
            query_id = row[0]
            for citation in citations:
                self._conn.execute(
                    """INSERT INTO rag_citations
                         (tenant_id, query_id, chunk_id, version_id, locator)
                       VALUES (%s, %s, %s, %s, %s)""",
                    (
                        context.tenant_id,
                        query_id,
                        UUID(citation.evidence_id),
                        UUID(citation.version_id),
                        Jsonb(citation.locator),
                    ),
                )
        # 显式提交：能力故障路径要靠这条记录留下 `ERROR`，而请求级连接在异常时整体回滚，
        # 不提交就等于"审计被 503 一起回滚掉了"（裁决要求 503 必须留下 ERROR 审计）。
        self._conn.commit()


__all__ = ["AnswerNotConfigured", "AnswerOutcome", "AnswerService"]
