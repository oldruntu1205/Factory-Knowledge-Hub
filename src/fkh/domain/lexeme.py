"""中文倒排分词适配器（当前为字级）。

PostgreSQL 默认的 `simple` / `english` 配置按空白和标点切词，中文整段会被当成
一个 token，导致关键词检索完全失效。L1 §5.2 要求 `lexeme` 由"明确的中文分词适配器"
写入，这里提供第一版实现：

- 在 CJK 字符之间插入空格，使 `to_tsvector('simple', ...)` 产出字级 token；
- 拉丁词与原样数字串保持完整（型号 24/29、单位 mm 不被拆碎）。

字级索引的代价是精度：查询"冷凝"会命中"冷冷凝"这类组合不存在的词，但召回不受影响。
这比"中文检不到"要好，且不依赖 zhparser 编译。Phase 2 引入真实分词器时替换本模块，
并同步重建 lexeme —— 索引与查询必须使用同一分词规则，否则永远匹配不上。

查询侧（2026-11-04 修正）：字级 token 之间必须用 **OR** 组合。此前直接用
`plainto_tsquery('simple', segment_cjk(q))` 会把每个汉字 AND 起来，自然语言问句
几乎不可能同时包含全部字，实测 50 条测评题 recall@10 只有 0.06（改 OR 后 0.66）。
"""

from __future__ import annotations

import re

_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\u3040-\u30ff\uac00-\ud7af]")
_WHITESPACE = re.compile(r"\s+")

TOKENIZER_ID = "cjk-char-v1"


def segment_cjk(text: str) -> str:
    """把文本切成以空格分隔的 token 序列，供 to_tsvector 使用。"""
    if not text:
        return ""
    spaced = _CJK.sub(lambda match: f" {match.group(0)} ", text)
    return _WHITESPACE.sub(" ", spaced).strip()


def build_query(text: str) -> str:
    """把自由文本转成 websearch 语法的 OR 查询表达式。

    三个决定：
    1. **OR 而不是 AND**：字级 token 用 AND 组合时，任何自然语言问句都几乎必然零命中；
    2. **用 websearch_to_tsquery 而不是 to_tsquery**：前者对任意输入都不抛语法错误，
       裸露的 `& | ! ( ) :` 会被当成普通字符，不会因为用户输入改变检索语义；
    3. **每个词项加双引号**：websearch 会把裸 `-word` 解释为排除、裸 `or` 当操作符；
       加引号后用户输入只可能被当成普通词项，避免"查询注入"。

    无有效词项（空白 / 纯标点）返回空串，调用方据此直接返回空结果，
    **不能**退化成"匹配所有内容"。
    """
    tokens = segment_cjk(text).split()
    quoted: list[str] = []
    for token in tokens:
        cleaned = token.replace('"', "").replace("\\", "")
        # 纯标点 token（"，，。"）不含任何字母/数字/汉字，交给 websearch 只会得到空 tsquery；
        # 这里直接丢弃，保证「纯标点 → 无有效词项 → 空结果」而不是任何形式的匹配。
        if cleaned and any(ch.isalnum() for ch in cleaned):
            quoted.append(f'"{cleaned}"')
    return " or ".join(quoted)


# 查询侧统一使用 websearch_to_tsquery('simple', build_query(query))。
# 不要把 build_query 的结果换成 to_tsquery：后者会解析操作符并要求转义。
QUERY_FUNCTION = "websearch_to_tsquery"