"""文档入库编排：解析 → 切片 → 事务落库 → 进入草稿态。

这一层刻意不做的事：
- 不自动审核。入库只把版本推进到 DRAFT，审核是独立职责（L1 §3.3）。
- 不生成向量。Embedding 模型待 ADR-04/06，写入 null 列并保留 embedding_model_id 为空，
  避免用错模型产生无法追溯的向量。
- 不吞掉复核原因。解析器标记的 `ReviewReason` 会随 SUBMIT 动作写入审计，
  让后续审核人知道这份草稿为什么需要人工看。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

import psycopg

from fkh.authz.policy import AclPolicy
from fkh.domain.chunking.chunker import build_chunks
from fkh.domain.chunking.tokenize import ApproxCjkTokenCounter, TokenCounter
from fkh.domain.chunking.types import ChunkingConfig
from fkh.domain.lexeme import TOKENIZER_ID as LEXEME_TOKENIZER_ID
from fkh.domain.parsing.registry import parse_document
from fkh.domain.state_machine import VersionStatus
from fkh.retrieval.embedding import Embedder
from fkh.workers.index_maintenance import mark_generation_ready
from fkh.workers.repository import DuplicateContentError, IngestionRepository

VALID_CLASSIFICATIONS = range(0, 5)
VALID_AUDIENCES = {"internal", "customer_public"}


@dataclass(frozen=True, slots=True)
class IngestionRequest:
    source_id: str
    filename: str
    content: bytes
    knowledge_key: str
    title: str
    domain: str
    owner_department: str
    classification: int
    audience: str
    # 读取该版本所需的知识范围。0007 明确不设默认值：'internal' 无法区分
    # 产品资料与培训材料，自动填 internal_product 会让培训内容被产品岗读到。
    required_scope: str
    acl_policy: AclPolicy
    actor_id: UUID
    source_object_key: str | None = None
    mime_type: str | None = None
    language: str = "zh-CN"
    # 目录货币必须显式给出（来自目录约定/元数据）；不给就不做结构化抽取，
    # 绝不"默认人民币"——价格口径错了比不抽更糟（2026-11-06 L1 裁决 §3.6）
    catalog_currency: str | None = None

    def __post_init__(self) -> None:
        if self.classification not in VALID_CLASSIFICATIONS:
            raise ValueError(f"classification 必须在 0..4 之间，当前为 {self.classification}")
        if self.audience not in VALID_AUDIENCES:
            raise ValueError(f"audience 必须是 {sorted(VALID_AUDIENCES)} 之一")
        if self.audience == "customer_public" and self.required_scope != "customer_public":
            raise ValueError("audience=customer_public 时 required_scope 必须为 customer_public")
        if self.audience == "internal" and self.required_scope not in {
            "internal_product",
            "internal_training",
        }:
            raise ValueError(
                "audience=internal 时 required_scope 必须是 internal_product 或 internal_training"
            )
        if not self.content:
            raise ValueError("content 为空，拒绝入库")
        if self.catalog_currency is not None and self.catalog_currency not in {"CNY"}:
            raise ValueError(f"不支持的目录货币：{self.catalog_currency}")


@dataclass(frozen=True, slots=True)
class IngestionResult:
    document_id: UUID
    version_id: UUID
    version_no: int
    status: VersionStatus
    parent_count: int
    child_count: int
    index_generation: int
    parser_id: str
    tokenizer_id: str
    review_reasons: tuple[str, ...]
    warnings: tuple[str, ...]

    @property
    def needs_human_review(self) -> bool:
        return bool(self.review_reasons)


def ingest_document(
    conn: psycopg.Connection[Any],
    tenant_id: UUID,
    request: IngestionRequest,
    *,
    config: ChunkingConfig | None = None,
    counter: TokenCounter | None = None,
    embedder: Embedder | None = None,
) -> IngestionResult:
    parsed = parse_document(
        request.content,
        source_id=request.source_id,
        filename=request.filename,
        mime_type=request.mime_type,
        language=request.language,
    )
    token_counter: TokenCounter = counter or ApproxCjkTokenCounter()
    chunking = build_chunks(parsed, counter=token_counter, config=config)

    if not chunking.parents:
        raise ValueError("解析后没有任何可索引内容，拒绝写入空版本")

    # 向量在事务外算完再入库：CPU 推理可能是分钟级，不能把连接和锁持有一整轮
    embeddings: list[list[float]] | None = None
    if embedder is not None:
        texts = [chunk.text for chunk in chunking.parents] + [
            chunk.text for chunk in chunking.children
        ]
        vectors = embedder.embed_documents(texts)
        if any(len(vector) != embedder.dimension for vector in vectors):
            raise ValueError(f"向量维度与 {embedder.model_id} 声明不一致")
        embeddings = vectors

    object_key = request.source_object_key or f"pending/{request.source_id}/{request.filename}"
    review_note = "ingest"
    if parsed.review_reasons:
        review_note = f"ingest; review={','.join(reason.value for reason in parsed.review_reasons)}"

    # 内部用 savepoint 保证原子性；提交由调用方决定
    with conn.transaction():
        repo = IngestionRepository(conn, tenant_id)

        document_id = repo.ensure_document(
            knowledge_key=request.knowledge_key,
            domain=request.domain,
            owner_department=request.owner_department,
        )

        duplicate = repo.find_version_by_content(document_id, parsed.source_sha256)
        if duplicate is not None:
            raise DuplicateContentError(duplicate)

        version_no = repo.next_version_no(document_id)
        acl_id = repo.create_acl(request.acl_policy)
        version_id = repo.create_version(
            document_id=document_id,
            version_no=version_no,
            title=request.title,
            classification=request.classification,
            audience=request.audience,
            required_scope=request.required_scope,
            content_sha256=parsed.source_sha256,
            source_object_key=object_key,
        )

        generation = repo.next_index_generation()
        parent_count, child_count = repo.insert_chunks(
            version_id=version_id,
            parents=chunking.parents,
            children=chunking.children,
            acl_policy_id=acl_id,
            index_generation=generation,
            tokenizer_id=chunking.tokenizer_id,
            # 切片 token 计数版本与 lexeme 分词器版本是两件事：
            # 前者决定切片边界，后者决定倒排能否与查询侧匹配
            lexeme_tokenizer_id=LEXEME_TOKENIZER_ID,
            parser_id=parsed.parser_id,
            language=request.language,
            embeddings=embeddings,
            embedding_model_id=embedder.model_id if embedder is not None else None,
        )

        # 目录结构化抽取：只在显式声明 domain=product_catalog 且给出货币时执行。
        # 同一事务内完成：抽取失败会随入库一起回滚，不会留下"有文档无事实"的半套状态。
        catalog_stats = None
        catalog_note: list[str] = []
        if request.domain == "product_catalog":
            if request.catalog_currency is None:
                catalog_note.append("catalog_skipped=no_currency")
            else:
                catalog_stats = repo.extract_catalog_items(
                    version_id=version_id, currency=request.catalog_currency
                )
                catalog_note.append(catalog_stats.as_warning())

        # 索引代就绪 + 可见水位前移（Rev2 §7.2：构建完成后才允许切换）
        total_chunks = parent_count + child_count
        # 只把该代标成 READY；**切换可见代是发布的动作**（§7.2：发布时校验后原子切换），
        # 这样未审核版本即使块已存在也不会被检索看到（纵深防御）。
        mark_generation_ready(conn, UUID(str(tenant_id)), generation, total_chunks)

        repo.transition(
            version_id=version_id,
            source=VersionStatus.RAW,
            target=VersionStatus.DRAFT,
            actor_id=request.actor_id,
            reason=review_note,
        )

    return IngestionResult(
        document_id=document_id,
        version_id=version_id,
        version_no=version_no,
        status=VersionStatus.DRAFT,
        parent_count=parent_count,
        child_count=child_count,
        index_generation=generation,
        parser_id=parsed.parser_id,
        tokenizer_id=chunking.tokenizer_id,
        review_reasons=tuple(reason.value for reason in parsed.review_reasons),
        warnings=chunking.warnings + parsed.warnings + tuple(catalog_note),
    )
