"""混合检索：向量召回 + 词法召回，用 RRF 融合。

RRF（Reciprocal Rank Fusion）只依赖**排名**，不依赖两路分数的量纲。
向量相似度（0–1 余弦）与 ts_rank_cd（无上界）本来就不可比，直接加权相加是错的；
按排名融合可以避开这个问题，也不需要为两路分数各调一个归一化参数。

融合顺序不是最终裁决：融合后仍要逐条做 ACL 判定。向量召回没有在 SQL 里做细粒度
权限过滤（策略是 JSONB，逐行判定放在应用层更实际），所以这里必须补齐，
否则就是"先召回后鉴权"——越权内容已经进入候选集了。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any
from uuid import UUID

import psycopg

from fkh.authz.policy import AclFormatError, AclPolicy, Decision, Subject, evaluate
from fkh.retrieval.embedding import Embedder, to_pgvector
from fkh.retrieval.lexical import (
    MAX_CANDIDATES,
    OVERFETCH,
    VISIBILITY_PREDICATE,
    LexicalRetriever,
    SearchHit,
    SearchOutcome,
    _snippet,
)

SCORING = "hybrid-rrf-v1"

_DENSE_SQL = f"""
SELECT c.id, c.version_id, v.document_id, c.chunk_text, c.locator, c.heading_path,
       p.policy_json, c.index_generation,
       1 - (c.embedding <=> %(vector)s::vector) AS similarity
  FROM knowledge_chunks c
  JOIN knowledge_versions v
    ON v.tenant_id = c.tenant_id AND v.id = c.version_id
  LEFT JOIN acl_policies p
    ON p.tenant_id = c.tenant_id AND p.id = c.acl_policy_id
 WHERE c.tenant_id = %(tenant)s
   AND c.chunk_kind = 'child'
   AND c.embedding IS NOT NULL
   AND c.embedding_model_id = %(model)s
   {VISIBILITY_PREDICATE}
 ORDER BY c.embedding <=> %(vector)s::vector
 LIMIT %(limit)s
"""


def reciprocal_rank_fusion(
    rankings: Sequence[Sequence[UUID]], *, k: int = 60
) -> dict[UUID, float]:
    """标准 RRF：score = Σ 1 / (k + rank)，rank 从 1 开始。

    k 默认 60 来自原论文。它决定"排名靠前"的权重集中度：k 越小越偏向头部。
    """
    if k <= 0:
        raise ValueError("rrf k 必须为正整数")
    scores: dict[UUID, float] = {}
    for ranking in rankings:
        for rank, doc_id in enumerate(ranking, start=1):
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank)
    return scores


def _policy(raw: Any) -> AclPolicy | None:
    if raw is None:
        return None
    try:
        return AclPolicy.from_json(raw)
    except AclFormatError:
        return None


class DenseRetriever:
    def __init__(self, conn: psycopg.Connection[Any], tenant_id: UUID, embedder: Embedder) -> None:
        self._conn = conn
        self._tenant = tenant_id
        self._embedder = embedder

    def search_ids(
        self, query: str, *, knowledge_scope: str, limit: int
    ) -> list[UUID]:
        self._conn.execute("SELECT set_config('app.tenant_id', %s, true)", (str(self._tenant),))
        vector = self._embedder.embed_query(query)
        rows = self._conn.execute(
            _DENSE_SQL,
            {
                "vector": to_pgvector(vector),
                "tenant": self._tenant,
                "scope": knowledge_scope,
                "model": self._embedder.model_id,
                "limit": limit,
            },
        ).fetchall()
        return [UUID(str(row[0])) for row in rows]


class HybridRetriever:
    def __init__(
        self,
        conn: psycopg.Connection[Any],
        tenant_id: UUID,
        *,
        embedder: Embedder,
        rrf_k: int = 60,
    ) -> None:
        self._conn = conn
        self._tenant = tenant_id
        self._embedder = embedder
        self._rrf_k = rrf_k

    def search(
        self,
        query: str,
        *,
        knowledge_scope: str,
        subject: Subject,
        max_results: int = 10,
    ) -> SearchOutcome:
        limit = min(max_results * OVERFETCH, MAX_CANDIDATES)

        dense_ids = DenseRetriever(self._conn, self._tenant, self._embedder).search_ids(
            query, knowledge_scope=knowledge_scope, limit=limit
        )
        lexical = LexicalRetriever(self._conn, self._tenant).search(
            query, knowledge_scope=knowledge_scope, subject=subject, max_results=max_results
        )
        lexical_ids = [hit.chunk_id for hit in lexical.hits]
        lexical_by_id = {hit.chunk_id: hit for hit in lexical.hits}

        fused = reciprocal_rank_fusion([dense_ids, lexical_ids], k=self._rrf_k)
        if not fused:
            return SearchOutcome(hits=(), candidates=0, filtered_out=0)

        ordered = sorted(fused.items(), key=lambda item: (-item[1], str(item[0])))

        # 词法侧已通过 ACL；向量侧只做了可见性过滤，这里补齐细粒度判定
        policies = self._load_policies([doc_id for doc_id, _ in ordered])
        hits: list[SearchHit] = []
        filtered_out = 0

        for doc_id, score in ordered:
            if len(hits) >= max_results:
                break
            existing = lexical_by_id.get(doc_id)
            if existing is not None:
                hits.append(
                    SearchHit(
                        chunk_id=existing.chunk_id,
                        version_id=existing.version_id,
                        document_id=existing.document_id,
                        text_snippet=existing.text_snippet,
                        snippet_truncated=existing.snippet_truncated,
                        locator=existing.locator,
                        heading_path=existing.heading_path,
                        score=score,
                    )
                )
                continue

            row = policies.get(doc_id)
            if row is None:
                filtered_out += 1
                continue
            decision, _ = evaluate(
                _policy(row[0]), subject, resource_tenant_id=str(self._tenant)
            )
            if decision is not Decision.ALLOW:
                filtered_out += 1
                continue

            snippet, truncated = _snippet(str(row[1]))
            hits.append(
                SearchHit(
                    chunk_id=doc_id,
                    version_id=UUID(str(row[2])),
                    document_id=UUID(str(row[3])),
                    text_snippet=snippet,
                    snippet_truncated=truncated,
                    locator=dict(row[4]) if row[4] is not None else None,
                    heading_path=tuple(row[5]) if row[5] is not None else None,
                    score=score,
                )
            )

        return SearchOutcome(hits=tuple(hits), candidates=len(fused), filtered_out=filtered_out)

    def _load_policies(self, ids: Sequence[UUID]) -> dict[UUID, tuple[Any, ...]]:
        if not ids:
            return {}
        rows = self._conn.execute(
            """SELECT c.id, c.chunk_text, c.version_id, v.document_id,
                      c.locator, c.heading_path, p.policy_json
                 FROM knowledge_chunks c
                 JOIN knowledge_versions v
                   ON v.tenant_id = c.tenant_id AND v.id = c.version_id
                 LEFT JOIN acl_policies p
                   ON p.tenant_id = c.tenant_id AND p.id = c.acl_policy_id
                WHERE c.tenant_id = %s AND c.id = ANY(%s)""",
            (self._tenant, list(ids)),
        ).fetchall()
        # 行序与 SQL 的 SELECT 顺序一致：(policy_json, chunk_text, version_id, document_id, locator, heading_path)
        return {
            UUID(str(row[0])): (row[6], row[1], row[2], row[3], row[4], row[5]) for row in rows
        }


__all__ = [
    "DenseRetriever",
    "HybridRetriever",
    "SCORING",
    "reciprocal_rank_fusion",
]
