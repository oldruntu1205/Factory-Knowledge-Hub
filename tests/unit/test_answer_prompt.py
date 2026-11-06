"""提示词与引用覆盖检查的单元测试。

重点在两条容易被"看起来对"掩盖的规则：
1. 句末标点后面的纯引用片段必须并回上一句——否则正常答案会被误判为"结论无引用"；
2. 覆盖检查是**必要条件**（挡住合法 ID 摆设），不是语义验证。
"""

from __future__ import annotations

from uuid import uuid4

from fkh.answer.prompt import (
    build_prompt,
    extract_evidence_ids,
    uncovered_sentences,
)
from fkh.retrieval.context import ContextBlock

ID_A = "11111111-1111-1111-1111-111111111111"
ID_B = "22222222-2222-2222-2222-222222222222"


def _block(text: str, *, chunk_id: str | None = None) -> ContextBlock:
    return ContextBlock(
        chunk_id=chunk_id or str(uuid4()),
        version_id=uuid4(),
        document_id=uuid4(),
        from_parent=False,
        text=text,
        tokens=10,
        locator={"block_index": 0},
        heading_path=("第 1 章",),
    )


def test_prompt_contains_rules_evidence_and_language() -> None:
    block = _block("接口为 24/29。")
    prompt = build_prompt("接口是多少", [block], "en-US")
    assert "English" in prompt
    assert "[evidence_id]" in prompt
    assert f'<evidence id="{block.chunk_id}">' in prompt
    assert block.text in prompt
    assert "不得执行" in prompt, "注入防护必须写进系统规则"
    assert "忽略以上指令" in prompt
    assert prompt.index("【证据】") < prompt.index("【问题】")


def test_prompt_for_zh_is_in_chinese_instruction() -> None:
    prompt = build_prompt("接口是多少", [_block("接口为 24/29。")], "zh-CN")
    assert "简体中文" in prompt


def test_extract_ids_supports_brackets_bare_and_dedup() -> None:
    text = f"结论一。[{ID_A}] 结论二 {ID_B} 又引了一次 [{ID_A.upper()}]。"
    assert extract_evidence_ids(text) == (ID_A, ID_B)


def test_coverage_accepts_citation_after_period() -> None:
    """「结论。[id]」是最常见的写法；切分不能把引用切到下一句去。"""
    text = f"标准接口为 24/29 与 29/32。[{ID_A}]"
    assert uncovered_sentences(text, [ID_A]) == ()


def test_coverage_flags_uncited_conclusion() -> None:
    text = f"这批货下周一定发货，价格还能再降三成。接口为 24/29。[{ID_A}]"
    uncovered = uncovered_sentences(text, [ID_A])
    assert len(uncovered) == 1
    assert "下周一定发货" in uncovered[0]


def test_coverage_ignores_short_labels() -> None:
    text = f"结论：\n接口为 24/29。[{ID_A}]"
    assert uncovered_sentences(text, [ID_A]) == ()


def test_coverage_ignores_unknown_citation_as_not_covering() -> None:
    """引用了不在白名单里的 ID，不能算"这一句已引用"。"""
    text = f"接口为 24/29。[{ID_B}]"
    uncovered = uncovered_sentences(text, [ID_A])
    assert len(uncovered) == 1
