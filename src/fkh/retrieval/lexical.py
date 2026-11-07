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

import math
from collections import Counter
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import psycopg

from fkh.authz.policy import AclPolicy, Decision, Subject, evaluate
from fkh.domain.lexeme import QUERY_FUNCTION, build_query, segment_cjk

# 分词器必须是写入 lexeme 时使用的同一个；换分词器要重建索引，否则永远匹配不上
# 生成 lexeme 的分词器版本，对应 knowledge_chunks.lexeme_tokenizer_id。
# 不要用 knowledge_chunks.tokenizer_id：那是切片 token 计数版本（approx-cjk-v1），
# 两者混用会让过滤条件永远不成立，检索静默返回空。
TOKENIZER_ID = "cjk-char-v1"

OVERFETCH = 4
MAX_CANDIDATES = 200
SNIPPET_CODE_POINTS = 400
# 查询构造由「逐字 AND」改为「字级 OR」（2026-11-04 L1 裁决 A）。
# 候选集合实质变化，版本号必须能区分新旧语义（判决 Q1：采用 lexical-v2）。
# v3：候选集仍由字级 OR 生成，但排序改为 BM25（字级）。原因见
# _evidence/l1_reply_lexical_bm25_20261106.md：ts_rank_cd 没有 IDF，
# 高频字会把重复的样板段落顶到判别词前面（实测「试管」查不到第 76 页）。
SCORING = "lexical-v3"
LEGACY_SCORING = "lexical-v2"
BM25_K1 = 1.2
BM25_B = 0.75

# 可见性谓词：租户、范围、状态三件套。词法与向量两路必须用同一份，
# 否则会出现"词法查得到、向量查不到"或反过来的口径漂移。
# Rev2 §3.4/§4.4（2026-11-07 审计纠偏）：外部客服只允许 `PUBLISHED_PUBLIC + customer_public`；
# 内部身份可以读 `APPROVED_INTERNAL`（不论 audience）。判据统一由
# `fkh.authz.policy.is_external_customer(allowed_scopes)` 给出，三处（检索/目录/证据）共用。
VISIBILITY_PREDICATE = """
   AND v.required_scope = %(scope)s
   AND ( (v.status = 'PUBLISHED_PUBLIC' AND v.audience = 'customer_public')
      OR (%(internal)s AND v.status = 'APPROVED_INTERNAL') )
"""

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
   AND c.lexeme_tokenizer_id = %(lexeme_tokenizer)s
   {VISIBILITY_PREDICATE}
 ORDER BY score DESC, c.id
 LIMIT %(limit)s
"""


_STATS_SQL = """
WITH visible AS (
  SELECT c.id, c.chunk_text, c.lexeme, p.policy_json, c.acl_policy_id,
         length(c.chunk_text) AS len
    FROM knowledge_chunks c
    JOIN knowledge_versions v
      ON v.tenant_id = c.tenant_id AND v.id = c.version_id
    LEFT JOIN acl_policies p
      ON p.tenant_id = c.tenant_id AND p.id = c.acl_policy_id
   WHERE c.tenant_id = %(tenant)s
     AND c.chunk_kind = 'child'
     AND c.lexeme_tokenizer_id = %(lexeme_tokenizer)s
     {visibility}
),
terms AS (SELECT unnest(%(terms)s::text[]) AS term)
SELECT acl_policy_id, policy_json, count(DISTINCT id) AS docs, sum(len) AS total_len,
       NULL::text AS term, NULL::bigint AS df
  FROM visible GROUP BY acl_policy_id, policy_json
UNION ALL
SELECT v.acl_policy_id, v.policy_json, NULL, NULL, t.term,
       count(*) FILTER (WHERE v.lexeme @@ plainto_tsquery('simple', t.term))
  FROM visible v CROSS JOIN terms t
 GROUP BY v.acl_policy_id, v.policy_json, t.term
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
    # 候选窗口与截断诊断：命中窗口上限时明确置位，便于排查"目标块没进窗"
    window: int = 0
    truncated: bool = False


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
        scoring: str = SCORING,
        internal: bool = True,
    ) -> SearchOutcome:
        self._bind_tenant()
        if max_results > MAX_CANDIDATES:
            # 窗口上限与请求条数冲突：显式拒绝，不悄悄少给结果
            raise ValueError(
                f"max_results={max_results} 超过候选窗口上限 {MAX_CANDIDATES}"
            )
        ts_query = build_query(query)
        if not ts_query:
            # 纯空白/标点：没有有效词项。不把空 tsquery 交给数据库，直接空结果；
            # 绝不能退化成「匹配所有内容」。
            return SearchOutcome(hits=(), candidates=0, filtered_out=0, window=0)

        limit = min(max_results * OVERFETCH, MAX_CANDIDATES)
        rows = self._conn.execute(
            _CANDIDATE_SQL,
            {
                "ts_query": ts_query,
                "tenant": self._tenant,
                "scope": knowledge_scope,
                "internal": internal,
                "lexeme_tokenizer": TOKENIZER_ID,
                "limit": limit,
            },
        ).fetchall()

        if scoring == SCORING:
            rows = self._rescore_bm25(
                query,
                rows,
                knowledge_scope=knowledge_scope,
                subject=subject,
                internal=internal,
            )
        elif scoring != LEGACY_SCORING:
            raise ValueError(f"未知的打分方式：{scoring}")

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
        return SearchOutcome(
            hits=tuple(hits),
            candidates=len(rows),
            filtered_out=filtered_out,
            window=limit,
            truncated=len(rows) >= limit,
        )

    def _rescore_bm25(
        self,
        query: str,
        rows: list[Any],
        *,
        knowledge_scope: str,
        subject: Subject,
        internal: bool = True,
    ) -> list[Any]:
        """按 BM25（字级）重排候选。

        统计量（N / avg_len / df）取自**同一可见性范围的全部块**，并按 ACL 过滤：
        不可见块不得影响可见结果的统计与排序（2026-11-06 L1 条件）。
        """
        tokens_by_row = {}
        terms: set[str] = set()
        for row in rows:
            tokens = _tokenize(str(row[3]))
            tokens_by_row[str(row[0])] = tokens
            terms.update(term for term in segment_cjk(query).split() if _is_term(term))

        n, avg_len, df = self._corpus_stats(
            knowledge_scope=knowledge_scope,
            subject=subject,
            terms=sorted(terms),
            internal=internal,
        )
        if n == 0 or avg_len <= 0:
            return rows

        query_terms = [term for term in segment_cjk(query).split() if _is_term(term)]
        scored: list[tuple[float, str, Any]] = []
        for row in rows:
            tokens = tokens_by_row[str(row[0])]
            doc_len = len(tokens) or 1
            tf = Counter(tokens)
            score = 0.0
            for term in query_terms:
                freq = tf.get(term, 0)
                if freq == 0:
                    continue
                idf = math.log((n - df.get(term, 0) + 0.5) / (df.get(term, 0) + 0.5) + 1.0)
                denom = freq + BM25_K1 * (1 - BM25_B + BM25_B * doc_len / avg_len)
                score += idf * freq * (BM25_K1 + 1) / denom
            scored.append((score, str(row[0]), row))

        # 分数相同时按 chunk_id 排，保证结果确定性
        scored.sort(key=lambda item: (-item[0], item[1]))
        result = []
        for score, _chunk_id, row in scored:
            enriched = list(row)
            enriched[8] = score
            result.append(enriched)
        return result

    def _corpus_stats(
        self,
        *,
        knowledge_scope: str,
        subject: Subject,
        terms: list[str],
        internal: bool = True,
    ) -> tuple[int, float, dict[str, int]]:
        rows = self._conn.execute(
            _STATS_SQL.format(visibility=VISIBILITY_PREDICATE),
            {
                "tenant": self._tenant,
                "scope": knowledge_scope,
                "internal": internal,
                "lexeme_tokenizer": TOKENIZER_ID,
                "terms": terms,
            },
        ).fetchall()

        n = 0
        total_len = 0
        df: dict[str, int] = {}
        for row in rows:
            decision, _reason = evaluate(
                _policy(row[1]), subject, resource_tenant_id=str(self._tenant)
            )
            if decision is not Decision.ALLOW:
                continue
            if row[4] is None:  # acl 级汇总行：docs / total_len
                n += int(row[2] or 0)
                total_len += int(row[3] or 0)
            elif row[5] is not None:
                df[str(row[4])] = df.get(str(row[4]), 0) + int(row[5])
        avg_len = (total_len / n) if n else 0.0
        return n, avg_len, df


def _is_term(term: str) -> bool:
    """只保留真实字符（CJK/字母/数字）。运算符与标点不参与 BM25 统计。

    `plainto_tsquery` 本身能吃任意文本，但让 `&`/`!` 进入 df 统计没有意义，
    还会让"运算符查询"看起来命中了不相关的块。
    """
    return bool(term) and all(char.isalnum() or "\u4e00" <= char <= "\u9fff" for char in term)


def _tokenize(text: str) -> list[str]:
    """与入库一致的字级分词（BM25 的 tf/文档长度都用它）。"""
    return segment_cjk(text).split()


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
    "BM25_B",
    "BM25_K1",
    "LEGACY_SCORING",
    "MAX_CANDIDATES",
    "OVERFETCH",
    "SCORING",
    "VISIBILITY_PREDICATE",
    "SNIPPET_CODE_POINTS",
    "TOKENIZER_ID",
    "LexicalRetriever",
    "SearchHit",
    "SearchOutcome",
]
