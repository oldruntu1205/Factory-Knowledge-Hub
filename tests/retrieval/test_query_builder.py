"""查询构造（字级 OR）的边界行为。

依据 2026-11-04 L1 裁决 A（`_evidence/l1_reply_lexical_and_fix_20261104.md`）：
沿用 `cjk-char-v1` 的词项语义，以 OR 组合有效词项；操作符与特殊字符必须被安全处理；
空白/纯标点/无有效词项返回空集合，**不得退化为全量匹配**。
"""

from __future__ import annotations

from fkh.domain.lexeme import QUERY_FUNCTION, build_query


def test_words_are_or_combined_not_anded() -> None:
    expression = build_query("冷凝管")
    assert " or " in expression
    assert "&" not in expression
    assert expression.count('"') == 6  # 三个词项各一对引号


def test_each_token_is_quoted_so_operators_cannot_change_semantics() -> None:
    # 用户输入里的操作符/符号只会变成普通词项或标点，不会被解释成 NOT / 括号
    expression = build_query("冷凝 & | ! ( ) :*")
    assert expression == '"冷" or "凝"'


def test_blank_and_punctuation_only_return_empty() -> None:
    assert build_query("") == ""
    assert build_query("   ") == ""
    assert build_query("，。！？；：") == ""


def test_single_character_query_is_supported() -> None:
    assert build_query("泵") == '"泵"'


def test_mixed_chinese_and_latin_terms_are_all_kept() -> None:
    assert build_query("ACL 下推") == '"ACL" or "下" or "推"'


def test_duplicate_terms_are_kept_and_harmless() -> None:
    # 重复词项不改变语义（websearch 去重），构造上保留原样即可
    assert build_query("冷凝 冷凝") == '"冷" or "凝" or "冷" or "凝"'


def test_quotes_are_stripped_from_tokens() -> None:
    # 未闭合引号不能把查询变成短语、也不能造成语法错误
    assert build_query('"未闭合') == '"未" or "闭" or "合"'


def test_query_function_is_websearch_not_to_tsquery() -> None:
    # to_tsquery 会解析操作符并要求调用方转义；websearch_to_tsquery 对任意输入都不报错
    assert QUERY_FUNCTION == "websearch_to_tsquery"
