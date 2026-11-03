"""中文倒排分词适配器（当前为字级）。

PostgreSQL 默认的 `simple` / `english` 配置按空白和标点切词，中文整段会被当成
一个 token，导致关键词检索完全失效。L1 §5.2 要求 `lexeme` 由"明确的中文分词适配器"
写入，这里提供第一版实现：

- 在 CJK 字符之间插入空格，使 `to_tsvector('simple', ...)` 产出字级 token；
- 拉丁词与原样数字串保持完整（型号 24/29、单位 mm 不被拆碎）。

字级索引的代价是精度：查询"冷凝"会命中"冷冷凝"这类组合不存在的词，但召回不受影响。
这比"中文检不到"要好，且不依赖 zhparser 编译。Phase 2 引入真实分词器时替换本模块，
并同步重建 lexeme —— 索引与查询必须使用同一分词规则，否则永远匹配不上。
"""

from __future__ import annotations

import re

_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\u3040-\u30ff\uac00-\ud7af]")
_WHITESPACE = re.compile(r"\s+")

TOKENIZER_ID = "cjk-char-v1"


def segment_cjk(text: str) -> str:
    """把文本切成以空格分隔的 token 序列，供 to_tsvector / plainto_tsquery 使用。"""
    if not text:
        return ""
    spaced = _CJK.sub(lambda match: f" {match.group(0)} ", text)
    return _WHITESPACE.sub(" ", spaced).strip()


# 查询侧统一使用 plainto_tsquery('simple', segment_cjk(query))：
# 它把输入当纯文本处理，自动用 AND 连接，不会因为 query 里出现 & | ! ( ) 而
# 产生语法错误或改变检索语义。不要手工拼 tsquery 字符串。
QUERY_FUNCTION = "plainto_tsquery"
