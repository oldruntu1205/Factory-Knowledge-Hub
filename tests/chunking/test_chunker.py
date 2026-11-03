"""父子切片测试。

重点验证边界：不超过 token 上限、不跨标题混切、大表按行拆且每行带表头、
child 能正确指回 parent。这些性质一旦破坏，检索质量和引用准确性会同时下降。
"""

from __future__ import annotations

import pytest

from fkh.domain.chunking.chunker import build_chunks
from fkh.domain.chunking.tokenize import ApproxCjkTokenCounter
from fkh.domain.chunking.types import ChunkingConfig
from fkh.domain.parsing.types import (
    BlockKind,
    Locator,
    ParsedBlock,
    ParsedDocument,
    TablePayload,
)

SHA = "0" * 64
SMALL = ChunkingConfig(
    parent_min_tokens=100,
    parent_max_tokens=200,
    child_min_tokens=50,
    child_max_tokens=100,
)

SENTENCE = "高硼硅玻璃冷凝管适用于回流提取，接口规格需按客户设备确认。"
LONG_TEXT = SENTENCE * 30
MEDIUM_TEXT = SENTENCE * 8


def _doc(*blocks: ParsedBlock) -> ParsedDocument:
    return ParsedDocument(
        source_id="s1",
        source_sha256=SHA,
        mime_type="text/markdown",
        parser_id="test@0",
        blocks=tuple(blocks),
    )


def _heading(text: str, level: int = 1, index: int = 0) -> ParsedBlock:
    return ParsedBlock(
        kind=BlockKind.HEADING,
        text=text,
        level=level,
        locator=Locator(block_index=index),
    )


def _para(text: str, index: int = 0) -> ParsedBlock:
    return ParsedBlock(kind=BlockKind.PARAGRAPH, text=text, locator=Locator(block_index=index))


def _table(headers: tuple[str, ...], rows: tuple[tuple[str, ...], ...], index: int = 0) -> ParsedBlock:
    payload = TablePayload(headers=headers, rows=rows)
    return ParsedBlock(
        kind=BlockKind.TABLE,
        text=payload.to_text(),
        table=payload,
        locator=Locator(block_index=index, table_index=index),
    )


# ---------------------------------------------------------------------------
# 边界
# ---------------------------------------------------------------------------
def test_parents_respect_max_tokens() -> None:
    result = build_chunks(_doc(_heading("规格"), _para(LONG_TEXT, 1)), config=SMALL)
    assert len(result.parents) > 1
    assert all(parent.token_count <= SMALL.parent_max_tokens for parent in result.parents)


def test_children_respect_max_tokens() -> None:
    result = build_chunks(_doc(_heading("规格"), _para(LONG_TEXT, 1)), config=SMALL)
    assert result.children
    assert all(child.token_count <= SMALL.child_max_tokens for child in result.children)


def test_every_child_points_to_existing_parent() -> None:
    result = build_chunks(_doc(_heading("规格"), _para(LONG_TEXT, 1)), config=SMALL)
    parent_ordinals = {parent.ordinal for parent in result.parents}
    for child in result.children:
        assert child.parent_ordinal is not None
        assert child.parent_ordinal in parent_ordinals


def test_no_content_is_lost() -> None:
    """拼接所有 parent 文本应覆盖原文，避免切片过程中静默丢段。

    切片会在句子边界插入换行，因此按"去空白后的子串"比较，
    而不是逐字节相等——后者会把正常的分隔符当成内容丢失。
    """
    result = build_chunks(_doc(_heading("规格"), _para(MEDIUM_TEXT, 1)), config=SMALL)
    compact = "".join(parent.text for parent in result.parents)
    normalize = lambda text: "".join(text.split())  # noqa: E731
    assert normalize(MEDIUM_TEXT) in normalize(compact)


def test_chunking_is_deterministic() -> None:
    document = _doc(_heading("规格"), _para(LONG_TEXT, 1))
    first = build_chunks(document, config=SMALL)
    second = build_chunks(document, config=SMALL)
    assert [c.content_sha256 for c in first.children] == [c.content_sha256 for c in second.children]


# ---------------------------------------------------------------------------
# 标题边界
# ---------------------------------------------------------------------------
def test_sections_do_not_mix_across_headings() -> None:
    document = _doc(
        _heading("冷凝管", 1, 0),
        _para(MEDIUM_TEXT, 1),
        _heading("烧瓶", 1, 2),
        _para(MEDIUM_TEXT, 3),
    )
    result = build_chunks(document, config=SMALL)
    paths = {parent.heading_path[0] for parent in result.parents}
    assert paths == {"冷凝管", "烧瓶"}

    for parent in result.parents:
        if parent.heading_path[0] == "冷凝管":
            assert "烧瓶" not in parent.text


def test_nested_heading_path_is_kept() -> None:
    """parent 按上层标题分组，child 保留到小节的完整路径。"""
    document = _doc(
        _heading("产品手册", 1, 0),
        _heading("冷凝管", 2, 1),
        _para(MEDIUM_TEXT, 2),
    )
    result = build_chunks(document, config=SMALL)
    assert any(child.heading_path == ("产品手册", "冷凝管") for child in result.children)
    assert any(parent.heading_path == ("产品手册", "冷凝管") for parent in result.parents)


def test_deep_subsections_are_merged_into_one_parent() -> None:
    """三级标题不应各自成块：契约类文档小节密集，逐节切会产生碎片。"""
    document = _doc(
        _heading("2. 架构", 2, 0),
        _heading("2.1 服务划分", 3, 1),
        _para(MEDIUM_TEXT, 2),
        _heading("2.2 部署边界", 3, 3),
        _para(MEDIUM_TEXT, 4),
    )
    roomy = ChunkingConfig(
        parent_min_tokens=100,
        parent_max_tokens=5000,
        child_min_tokens=100,
        child_max_tokens=2000,
    )
    result = build_chunks(document, config=roomy)

    assert len(result.parents) == 1
    parent = result.parents[0]
    assert parent.heading_path == ("2. 架构",)
    assert "2.1 服务划分" in parent.text
    assert "2.2 部署边界" in parent.text


# ---------------------------------------------------------------------------
# 表格
# ---------------------------------------------------------------------------
def test_large_table_split_by_row_keeps_header() -> None:
    rows = tuple((f"CG-{i:03d}", "24/29", f"{300 + i}") for i in range(60))
    document = _doc(_heading("规格表"), _table(("型号", "接口", "长度"), rows, 1))
    result = build_chunks(document, config=SMALL)

    assert len(result.parents) > 1
    for parent in result.parents[1:]:  # 第一个 parent 含标题，可能不含表头
        assert "型号" in parent.text or "接口" in parent.text


def test_small_table_stays_intact() -> None:
    rows = (("CG-001", "24/29", "300"), ("CG-002", "29/32", "400"))
    document = _doc(_heading("规格表"), _table(("型号", "接口", "长度"), rows, 1))
    result = build_chunks(document, config=SMALL)
    combined = "\n".join(parent.text for parent in result.parents)
    assert "CG-001" in combined and "CG-002" in combined


# ---------------------------------------------------------------------------
# 告警与空文档
# ---------------------------------------------------------------------------
def test_short_document_emits_warning() -> None:
    document = _doc(_heading("短"), _para("一句话。", 1))
    result = build_chunks(document, config=SMALL)
    assert any("低于下限" in warning for warning in result.warnings)
    # 告警不等于拒绝：内容仍应产出，交由人工抽查
    assert result.parents and result.children


def test_empty_document_returns_no_chunks() -> None:
    result = build_chunks(_doc(), config=SMALL)
    assert result.parents == ()
    assert result.children == ()
    assert "无可切片内容" in result.warnings[0]


def test_default_config_is_within_l1_experimental_range() -> None:
    config = ChunkingConfig()
    assert (config.child_min_tokens, config.child_max_tokens) == (250, 450)
    assert (config.parent_min_tokens, config.parent_max_tokens) == (800, 1500)


def test_invalid_config_rejected() -> None:
    with pytest.raises(ValueError, match="child_max_tokens"):
        ChunkingConfig(parent_min_tokens=100, parent_max_tokens=200, child_min_tokens=50, child_max_tokens=500)


# ---------------------------------------------------------------------------
# token 计数
# ---------------------------------------------------------------------------
def test_token_counter_cjk_and_empty() -> None:
    counter = ApproxCjkTokenCounter()
    assert counter.count("") == 0
    assert counter.count("冷凝管") == 3
    assert counter.count("Condenser") == 2
    assert counter.id == "approx-cjk-v1"
