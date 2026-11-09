"""检索可见性谓词与分词器版本（检索、统计、刷新共用的唯一来源）。

`VISIBILITY_PREDICATE` 必须同时被候选 SQL、BM25 live 统计、统计表刷新使用；
复制一份字符串就会产生“统计口径与候选口径漂移”，这是本模块存在的唯一理由。
"""

from __future__ import annotations

TOKENIZER_ID = "cjk-char-v1"

VISIBILITY_PREDICATE = """
   AND v.required_scope = %(scope)s
   AND ( (v.status = 'PUBLISHED_PUBLIC' AND v.audience = 'customer_public')
      OR (%(internal)s AND v.status = 'APPROVED_INTERNAL') )
"""
