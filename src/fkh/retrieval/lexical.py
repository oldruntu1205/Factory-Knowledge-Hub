"""词法检索：候选召回 + ACL 前置过滤。

为什么是两段式（SQL 召回 → 应用层 ACL 过滤）：
候选的可见性由「Deny 优先 / 无匹配拒绝 / 策略缺失拒绝 / 跨租户拒绝」四条规则共同决定，
它们是四维集合运算。把它翻译成 SQL 条件会得到与应用层求值器**两套语义**，
一处改动不同步就是越权或漏检。所以 SQL 只负责可判定的硬条件（租户、状态、范围、
分词器、全文匹配），ACL 一律回到 `authz.policy.evaluate`。

代价是**候选挤占**：`max_results` 只是返回上限，不保证填满，也不保证拿到全部可见内容里的
完整 top-K。无权内容可能把有权内容挤出候选窗口。因此规格里不得承诺
「空结果说明没有可见相关内容」，也不得承诺完全隐藏无权内容的存在。

本轮**不返回 parent 块**：parent 的文本聚合了它的全部 child，即使不做「恢复」，
直接返回 parent 片段也可能泄露无权 child 的内容。父块全文只走 `GET /v1/evidence/{id}`
（那里强制「全部 child 可见」才可恢复）。

本轮**不返回 score**：排序保证即可，原始分不作为对外字段。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

import psycopg

from fkh.authz.policy import AclPolicy, Decision, Subject, evaluate
from fkh.domain.lexeme import QUERY_FUNCTION, segment_cjk

# 分词器必须是写入 lexeme 时使用的同一个；换分词器要重建索引，否则永远匹配不上
TOKENIZER_ID = "cjk-char-v1"

OVERFETCH = 4
MAX_CANDIDATES = 200
SNIPPET_CODE_POINTS = 400
SCORING = "lexical-v1"

_CANDIDATE_SQL = f"""
SELECT c.id, c.version_id, v.document_id, c.chunk_text, c.locator, c.heading_path,
       p.policy_json, c.index_generation,
       ts_rank_cd(c.lexeme, tq.q) AS score
  FROM knowledge_chunks c
  JOIN knowledge_versions v
    ON v.tenant_id = c.tenant_id AND v.id = c.version_id
  LEFT JOIN acl_policies p
    ON p.tenant_id = c.tenant_id AND p.id = c.acl_policy_id
  CROSS JOIN (SELECT {QUERY_FUNCTION}('simple', %(ts_query)s) AS q) tq
 WHERE c.tenant_id = %(tenant)s
   AND c.lexeme @@ tq.q
   AND c.chunk_kind = 'child'
   AND c.tokenizer_id = %(tokenizer)s
   AND v.required_scope = %(scope)s
   AND ( (v.status = 'APPROVED_INTERNAL' AND v.audience = 'internal')
      OR (v.status = 'PUBLISHED_PUBLIC' AND v.audience = 'customer_public') )
 ORDER BY score DESC, c.id
 LIMIT %(limit)s
"""


@dataclass(frozen=True, slots=True)
class SearchHit:
    chunk_id: UUID
    version_id: UUID
    document_id: UUID
    text_snippet: str
    snippet_truncated: bool
    locator: dict[str, Any] | None
    heading_path: tuple[str, ...] | None
    score: float


@dataclass(frozen=True, slots=True)
class SearchOutcome:
    hits: tuple[SearchHit, ...]
    candidates: int
    filtered_out: int


class LexicalRetriever:
    def __init__(self, conn: psycopg.Connection[Any], tenant_id: UUID) -> None:
        self._conn = conn
        self._tenant = tenant_id
        self._bind_tenant()

    def _bind_tenant(self) -> None:
        self._conn.execute("SELECT set_config('app.tenant_id', %s, true)", (str(self._tenant),))

    def search(
        self,
        query: str,
        *,
        knowledge_scope: str,
        subject: Subject,
        max_results: int = 10,
    ) -> SearchOutcome:
        self._bind_tenant()
        ts_query = segment_cjk(query)
        if not ts_query:
            # 纯空白/标点：分词后没有词项。不把空 tsquery 交给数据库，直接空结果
            return SearchOutcome(hits=(), candidates=0, filtered_out=0)

        limit = min(max_results * OVERFETCH, MAX_CANDIDATES)
        rows = self._conn.execute(
            _CANDIDATE_SQL,
            {
                "ts_query": ts_query,
                "tenant": self._tenant,
                "scope": knowledge_scope,
                "tokenizer": TOKENIZER_ID,
                "limit": limit,
            },
        ).fetchall()

        hits: list[SearchHit] = []
        filtered_out = 0
        for row in rows:
            decision, _reason = evaluate(
                _policy(row[6]), subject, resource_tenant_id=str(self._tenant)
            )
            if decision is not Decision.ALLOW:
                filtered_out += 1
                continue
            if len(hits) >= max_results:
                # 候选窗口内已凑够条数：剩余行不再处理，但也不再放宽任何判定
                continue
            snippet, truncated = _snippet(str(row[3]))
            hits.append(
                SearchHit(
                    chunk_id=UUID(str(row[0])),
                    version_id=UUID(str(row[1])),
                    document_id=UUID(str(row[2])),
                    text_snippet=snippet,
                    snippet_truncated=truncated,
                    locator=dict(row[4]) if row[4] is not None else None,
                    heading_path=tuple(str(part) for part in row[5]) if row[5] is not None else None,
                    score=float(row[8]),
                )
            )
        return SearchOutcome(hits=tuple(hits), candidates=len(rows), filtered_out=filtered_out)


def _snippet(text: str) -> tuple[str, bool]:
    """按 Unicode 码点截断。

    不能用 `text[:400]` 直接切：Python 的切片单位是码点，本身是安全的，但若上游把
    补充平面字符切成代理对（surrogate pair）再拼回来，截断会砸出半个字符。
    这里显式按码点计数并保证截断点不在代理项中间。
    """
    if len(text) <= SNIPPET_CODE_POINTS:
        return text, False
    cut = SNIPPET_CODE_POINTS
    # 若截断点前一个字符是高位代理（0xD800–0xDBFF），退一格避免拆开代理对
    if 0xD800 <= ord(text[cut - 1]) <= 0xDBFF:
        cut -= 1
    return text[:cut], True


def _policy(raw: Any) -> AclPolicy | None:
    if raw is None:
        return None
    return AclPolicy.from_json(raw)


__all__ = [
    "MAX_CANDIDATES",
    "OVERFETCH",
    "SCORING",
    "SNIPPET_CODE_POINTS",
    "TOKENIZER_ID",
    "LexicalRetriever",
    "SearchHit",
    "SearchOutcome",
]
