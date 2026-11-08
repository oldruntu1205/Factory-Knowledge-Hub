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

import logging
import re
import time
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

from fkh.answer.consistency import RULE_VERSION as CONSISTENCY_RULE
from fkh.answer.consistency import ClaimCheck, check_answer
from fkh.answer.prompt import build_prompt, extract_citations
from fkh.authz.policy import is_external_customer
from fkh.catalog.lookup import chunk_contains_row, find_catalog_item
from fkh.domain.catalog import count_distinct_codes, detect_catalog_intent
from fkh.domain.catalog_rules import load_rule
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

CATALOG_MODEL_ID = "catalog-exact:v1"

logger = logging.getLogger(__name__)

# 展示层：把内联的内部 id 转成用户可读的引用序号（不裸露 chunk_id）
_INLINE_ID = re.compile(r"\[([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\]")


class AnswerNotConfigured(RuntimeError):
    """没有可用的本地推理能力（provider=none / 未配模型 / 模型不存在 / 缺审计密钥）。"""


@dataclass(frozen=True, slots=True)
class AnswerOutcome:
    payload: dict[str, Any]
    status: AnswerStatus
    model_id: str | None


_HANDOFF_TEXT = (
    "这类问题涉及价格、交期、质量承诺或定制可行性，需要业务人员确认后回复，"
    "系统不提供自动承诺。我可以先提供该产品的公开规格与使用说明，商务条款请由销售同事答复。"
)

# 商务承诺类触发词（确定性、可审计；只对"外部客服"身份生效）
_COMMITMENT_KEYWORDS = (
    # 价格
    "单价", "价格", "报价", "多少钱", "价钱", "价位", "折扣", "优惠",
    "price", "quote", "quotation", "discount", "cost",
    # 交期 / 现货
    "交期", "交货", "货期", "多久能发", "多久发货", "什么时候发", "现货",
    "能到", "几号到", "lead time", "delivery", "in stock", "stock",
    # 采购条件
    "起订", "最小起订", "moq", "minimum order",
    # 质量承诺
    "质保", "保修", "保证", "承诺", "包换", "包退", "赔偿", "赔付",
    "warranty", "guarantee", "refund", "compensation",
    # 定制 / 加工
    "定制", "定做", "开模", "改型", "图纸", "加工", "订做",
    "custom", "customization", "oem", "odm", "drawing",
)

# 语义兜底：确定性词表只证明"命中必须转人工"，**不能证明未命中就可以自动回答**。
# L1 终审（2026-11-07）要求：未命中时做语义识别，疑似/识别失败/不确定一律转人工；
# 模型只能"加严"，永远不能解除确定性拦截。
_COMMITMENT_CLASSIFIER_PROMPT = (
    "你是一个意图分类器。判断下面的客户问题是否在询问以下任一项："
    "价格或折扣、交期或现货、质量承诺或保修、定制或开模、最小起订量等采购条件。\n"
    "只输出一个字：是 / 否。不确定时必须输出：是。\n\n"
    "问题：{query}\n"
)


def _is_external_customer(context: SearchContext) -> bool:
    """外部客服判定（唯一来源在 authz.policy，这里只是就地取用）。"""
    return is_external_customer(context.allowed_scopes)


def _is_commitment_request(query: str) -> bool:
    return any(keyword in query.lower() for keyword in _COMMITMENT_KEYWORDS)


def _external_needs_human(query: str, generator: TextGenerator | None) -> bool:
    """外部客服的商务类问题判定（fail-closed）。

    1) 确定性词表命中 → 转人工（可解释、可回归）；
    2) 未命中 → 语义识别；只有明确"否"才放行；
       识别失败 / 抛错 / 无模型 → 转人工（宁可多转，不可漏拦）。
    """
    if _is_commitment_request(query):
        return True
    if generator is None:
        return True
    try:
        verdict = generator.generate(
            _COMMITMENT_CLASSIFIER_PROMPT.format(query=query), max_tokens=8, temperature=0.0
        )
    except GenerationError:
        return True
    text = (getattr(verdict, "text", "") or "").strip()
    return not text.startswith("否")


def _unsupported_counts(check: ClaimCheck | None) -> dict[str, int | None] | None:
    """返回未支持的**数量**（不返回原文，避免泄漏证据细节）。无 claim 记 None=不适用。"""
    if check is None or not check.applicable:
        return None
    return {
        "codes": (check.codes_total - check.codes_supported) if check.codes_total else None,
        "numbers": (check.numbers_total - check.numbers_supported) if check.numbers_total else None,
    }


def _render_citations(answer_text: str, citations: Any) -> str:
    """把正文里的内联内部 id 换成用户可读的引用序号（裁决：不得裸露内部 id）。"""
    index = {citation.evidence_id: position + 1 for position, citation in enumerate(citations)}

    def replace(match: re.Match[str]) -> str:
        key = match.group(1).lower()
        if key in index:
            return f"[{index[key]}]"
        return "[?]"

    return _INLINE_ID.sub(replace, answer_text)


def _refusal_reason(
    plan: AnswerPlan,
    proposed_ids: tuple[str, ...],
    explicit_none: bool,
    answer_text: str,
) -> str:
    """把"为什么没答"拆成可检索的原因（按裁决只在日志区分，不扩审计表结构）。"""
    if plan.status is AnswerStatus.HUMAN_REVIEW_REQUIRED:
        return "invalid_ids"
    if not answer_text.strip():
        return "empty_answer"
    if not proposed_ids:
        return "explicit_none" if explicit_none else "no_citation_line"
    return "no_valid_citation"


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
        only_version_ids: tuple[UUID, ...] | None = None,
        max_classification: int | None = None,
    ) -> AnswerOutcome:
        trace_id = context.trace_id
        # 审计只能存摘要：先算哈希，算不出来（无密钥）就不具备审计条件
        query_hash = query_fingerprint(self._hash_key, query) if self._hash_key else None

        # 外部客服（只有 customer_public 数据范围）的商务承诺类问题必须转人工（Rev2 §4.5）：
        # 价格、交期、质量承诺、定制可行性**都不许由系统直接给答案**，也不许走
        # 目录精确通道——那条通道会自动把单价读出来。
        if _is_external_customer(context) and _external_needs_human(query, self._generator):
            if query_hash is not None:
                self._audit(
                    context=context,
                    knowledge_scope=knowledge_scope,
                    status="HUMAN_REVIEW_REQUIRED",
                    refusal_code=RefusalCode.HUMAN_REVIEW_REQUIRED,
                    model_id=None,
                    citations=(),
                    trace_id=trace_id,
                    query_hash=query_hash,
                    route="handoff",
                    route_reason="external_customer_commitment",
                )
            plan = AnswerPlan(
                status=AnswerStatus.HUMAN_REVIEW_REQUIRED,
                citations=(),
                requires_human_review=True,
                refusal_code=RefusalCode.HUMAN_REVIEW_REQUIRED,
            )
            return AnswerOutcome(
                payload=to_response(
                    plan,
                    answer_text=_HANDOFF_TEXT,
                    trace_id=trace_id,
                    index_generation=0,
                    literal_support=None,
                ),
                status=plan.status,
                model_id=None,
            )

        # 目录精确通道：确定性规则，不经过模型（即使 provider=none 也应可用）
        catalog_outcome, catalog_reason = self._catalog_answer(
            query,
            knowledge_scope=knowledge_scope,
            context=context,
            only_version_ids=only_version_ids,
            max_classification=max_classification,
            query_hash=query_hash,
        )
        if catalog_outcome is not None:
            return catalog_outcome

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
                    route="rag",
                    route_reason=catalog_reason,
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
            version_ids=only_version_ids,
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
                context,
                knowledge_scope,
                plan,
                model_id=None,
                trace_id=trace_id,
                query_hash=query_hash,
                route_reason=catalog_reason,
            )
            return AnswerOutcome(
                payload=to_response(plan, answer_text="", trace_id=trace_id, index_generation=0),
                status=plan.status,
                model_id=None,
            )

        answer_rule = load_rule(self._conn, context.tenant_id)
        prompt = build_prompt(
            query,
            assembly.blocks,
            language,
            code_pattern=answer_rule.code_pattern if answer_rule is not None else None,
        )
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
                route="rag",
                route_reason=catalog_reason,
            )
            raise

        blocks = assembly.blocks
        knowledge_id_by_version = {str(block.version_id): str(block.document_id) for block in blocks}
        # 2026-11-06 业务决策 A：改为「正文 + 引用清单」。
        # 引用清单行从展示正文剥离；逐句覆盖不再作为闸门（7B–14B 不遵守内联标注），
        # 但"零有效引用"与"白名单外伪造 id"两条拦截保持不变。
        answer_text, proposed_ids, explicit_none = extract_citations(generated.text)
        if explicit_none and proposed_ids:
            # 模型写了"引用：无"，正文却带合法内联引用：以合并结果为准，并记下协议冲突
            logger.warning(
                "answer protocol conflict: explicit none but inline ids present trace_id=%s",
                trace_id,
            )
        build = build_citations(
            proposed_ids,
            blocks,
            knowledge_id_by_version=knowledge_id_by_version,
        )
        plan = decide(blocks, build, answer_text=answer_text)

        consistency: ClaimCheck | None = None
        if plan.status is AnswerStatus.ANSWERED:
            # 2026-11-06 裁决 ①：只跟**有效引用的证据块**比对，不回退到全部证据。
            cited = {citation.evidence_id for citation in plan.citations}
            cited_texts = [block.text for block in blocks if str(block.chunk_id) in cited]
            started = time.perf_counter()
            consistency = check_answer(answer_text, cited_texts)
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            if not consistency.supported:
                # 不一致 ≠ 一定错（可能是改写/单位换算），所以保留答案但要求人工复核
                plan = AnswerPlan(
                    status=AnswerStatus.HUMAN_REVIEW_REQUIRED,
                    citations=plan.citations,
                    requires_human_review=True,
                    refusal_code=RefusalCode.UNSUPPORTED_CLAIM,
                )
                logger.info(
                    "answer unsupported claims: reasons=%s codes=%s numbers=%s trace_id=%s",
                    ",".join(consistency.reason_codes),
                    len(consistency.unsupported_codes),
                    len(consistency.unsupported_numbers),
                    trace_id,
                )
            consistency_ms = elapsed_ms
        else:
            consistency_ms = 0

        if plan.status is not AnswerStatus.ANSWERED:
            logger.info(
                "answer not answered: status=%s code=%s reason=%s trace_id=%s",
                plan.status.value,
                plan.refusal_code.value if plan.refusal_code else None,
                _refusal_reason(plan, proposed_ids, explicit_none, answer_text),
                trace_id,
            )

        payload = to_response(
            plan,
            answer_text=_render_citations(answer_text, plan.citations),
            trace_id=trace_id,
            index_generation=self._index_generation(context.tenant_id, blocks),
            unsupported=_unsupported_counts(consistency),
            literal_support=consistency.to_public() if consistency is not None else None,
        )
        self._persist(
            context,
            knowledge_scope,
            plan,
            model_id=generated.model_id,
            trace_id=trace_id,
            query_hash=query_hash,
            route="rag",
            route_reason=catalog_reason,
            consistency=consistency,
            consistency_ms=consistency_ms,
        )
        return AnswerOutcome(payload=payload, status=plan.status, model_id=generated.model_id)

    # ---- 内部 --------------------------------------------------------------
    def _catalog_answer(
        self,
        query: str,
        *,
        knowledge_scope: str,
        context: SearchContext,
        only_version_ids: tuple[UUID, ...] | None = None,
        max_classification: int | None = None,
        query_hash: str | None,
    ) -> tuple[AnswerOutcome | None, str | None]:
        """确定性目录通道。返回 (结果, 未路由原因)。

        只有「恰好一个货号 + 明确在问价格/规格」才会接管；其余一律交回 RAG，
        并把"为什么没路由"写进审计（不写原文，只写短枚举）。
        """
        # 读路径：规则缺失时用工厂正则兜底（缺配置不阻断已有目录的作答）
        rule = load_rule(self._conn, context.tenant_id)
        code_pattern = rule.code_pattern if rule is not None else None
        intent = detect_catalog_intent(query, code_pattern=code_pattern)
        if intent is None:
            codes = count_distinct_codes(query, code_pattern=code_pattern)
            reason = (
                "no_code"
                if codes == 0
                else ("multi_code" if codes > 1 else "code_without_intent")
            )
            return None, reason

        if self._hash_key is None:
            # 没有审计密钥就无法满足"审计写不进去不算成功"，直接按能力未就绪处理
            raise AnswerNotConfigured("未配置 FKH_RETRIEVAL_HASH_KEY，无法写合规审计")

        lookup = find_catalog_item(
            self._conn,
            context.tenant_id,
            intent.code,
            knowledge_scope=knowledge_scope,
            subject=context.subject,
            internal=not is_external_customer(context.allowed_scopes),
            # 培训问答与普通问答共用"当前任务的课程来源 + 分级"（2026-11-08）
            version_ids=only_version_ids,
            max_classification=max_classification,
        )

        if lookup.ambiguous:
            plan = AnswerPlan(
                status=AnswerStatus.REFUSED,
                citations=(),
                requires_human_review=True,
                refusal_code=RefusalCode.CONFLICTING_EVIDENCE,
            )
            payload = to_response(
                plan,
                answer_text="同一货号在不同目录版本里对应不同规格或价格，需要人工确认后再回答。",
                trace_id=context.trace_id,
                index_generation=0,
            )
            self._persist(
                context,
                knowledge_scope,
                plan,
                model_id=CATALOG_MODEL_ID,
                trace_id=context.trace_id,
                query_hash=query_hash,
                route="catalog_exact",
                route_reason="ambiguous",
            )
            return AnswerOutcome(payload=payload, status=plan.status, model_id=CATALOG_MODEL_ID), None

        match = lookup.match
        if match is None:
            plan = AnswerPlan(
                status=AnswerStatus.REFUSED,
                citations=(),
                requires_human_review=False,
                refusal_code=RefusalCode.NO_EVIDENCE,
            )
            payload = to_response(
                plan,
                answer_text="",
                trace_id=context.trace_id,
                index_generation=0,
            )
            self._persist(
                context,
                knowledge_scope,
                plan,
                model_id=CATALOG_MODEL_ID,
                trace_id=context.trace_id,
                query_hash=query_hash,
                route="catalog_exact",
                route_reason="not_found",
            )
            return AnswerOutcome(payload=payload, status=plan.status, model_id=CATALOG_MODEL_ID), None

        if not chunk_contains_row(match.chunk_text, match.raw_text):
            # 结构化与原文不一致：不得用结构化值硬答
            plan = AnswerPlan(
                status=AnswerStatus.HUMAN_REVIEW_REQUIRED,
                citations=(),
                requires_human_review=True,
                refusal_code=RefusalCode.HUMAN_REVIEW_REQUIRED,
            )
            payload = to_response(
                plan,
                answer_text="目录条目与原文不一致，已转人工复核。",
                trace_id=context.trace_id,
                index_generation=0,
            )
            self._persist(
                context,
                knowledge_scope,
                plan,
                model_id=CATALOG_MODEL_ID,
                trace_id=context.trace_id,
                query_hash=query_hash,
                route="catalog_exact",
                route_reason="inconsistent",
            )
            return AnswerOutcome(payload=payload, status=plan.status, model_id=CATALOG_MODEL_ID), None

        citation = Citation(
            evidence_id=str(match.chunk_id),
            knowledge_id=str(match.document_id),
            version_id=str(match.version_id),
            locator=match.locator,
        )
        plan = AnswerPlan(
            status=AnswerStatus.ANSWERED,
            citations=(citation,),
            requires_human_review=False,
            refusal_code=None,
        )
        external = is_external_customer(context.allowed_scopes)
        if intent.kind == "price" and external:
            # 防御纵深：外部客户的询价本应在进入通道前转人工（关键词 + 语义兜底），
            # 万一漏到这里，也绝不把单价读出来。
            plan = AnswerPlan(
                status=AnswerStatus.HUMAN_REVIEW_REQUIRED,
                citations=(),
                requires_human_review=True,
                refusal_code=RefusalCode.HUMAN_REVIEW_REQUIRED,
            )
            payload = to_response(
                plan,
                answer_text=_HANDOFF_TEXT,
                trace_id=context.trace_id,
                index_generation=0,
            )
            self._persist(
                context,
                knowledge_scope,
                plan,
                model_id=CATALOG_MODEL_ID,
                trace_id=context.trace_id,
                query_hash=query_hash,
                route="catalog_exact",
                route_reason="not_found",
            )
            return AnswerOutcome(
                payload=payload, status=plan.status, model_id=CATALOG_MODEL_ID
            ), None

        if intent.kind == "price":
            answer_text = (
                f"{match.code} 的单价为 {match.raw_price}（{match.currency}），规格：{match.spec}。"
            )
        elif external:
            # 外部客户只拿"允许自动作答的规格事实"；价格/交期/承诺另行转人工
            answer_text = f"{match.code} 的规格为 {match.spec}。"
        else:
            answer_text = (
                f"{match.code} 的规格为 {match.spec}；单价 {match.raw_price}（{match.currency}）。"
            )
        payload = to_response(
            plan,
            answer_text=answer_text,
            trace_id=context.trace_id,
            index_generation=self._catalog_index_generation(
                context.tenant_id, match.version_id
            ),
        )
        self._persist(
            context,
            knowledge_scope,
            plan,
            model_id=CATALOG_MODEL_ID,
            trace_id=context.trace_id,
            query_hash=query_hash,
            route="catalog_exact",
            route_reason="accepted",
        )
        return AnswerOutcome(payload=payload, status=plan.status, model_id=CATALOG_MODEL_ID), None

    def _catalog_index_generation(self, tenant_id: UUID, version_id: UUID) -> int:
        row = self._conn.execute(
            """SELECT coalesce(max(index_generation), 0)
                 FROM knowledge_chunks
                WHERE tenant_id = %s AND version_id = %s""",
            (tenant_id, version_id),
        ).fetchone()
        return int(row[0]) if row is not None else 0

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
        route: str = "rag",
        route_reason: str | None = None,
        consistency: Any = None,
        consistency_ms: int | None = None,
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
            route=route,
            route_reason=route_reason,
            consistency=consistency,
            consistency_ms=consistency_ms,
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
        route: str,
        route_reason: str | None,
        consistency: Any = None,
        consistency_ms: int | None = None,
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
                      trace_id, model_id, route, route_reason,
                      consistency_rule, codes_total, codes_supported,
                      numbers_total, numbers_supported, consistency_ms)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                           %s, %s, %s, %s, %s, %s)
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
                    route,
                    route_reason,
                    CONSISTENCY_RULE if consistency is not None else None,
                    consistency.codes_total if consistency is not None else None,
                    consistency.codes_supported if consistency is not None else None,
                    consistency.numbers_total if consistency is not None else None,
                    consistency.numbers_supported if consistency is not None else None,
                    consistency_ms,
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
