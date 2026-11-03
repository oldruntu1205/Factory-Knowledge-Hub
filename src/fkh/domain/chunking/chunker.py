"""父子切片。

策略：
1. 以标题为分段边界，绝不把两个标题下的内容混进同一个 parent；
2. parent 按 token 预算打包，超限的单个块先拆开再打包；
3. child 从 parent 的块序列切出，保留覆盖到的块定位，父块内不跨标题；
4. 大表格按行拆，每行重复表头与单位——单行数据脱离列名没有意义。

刻意不做的事：不按固定字符数切。字符数与 token 数在中文技术文档里偏差很大，
按字符切会让"24/29 接口"这类短行被切断，或让长表格整块塞进一个 child。
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence

from fkh.domain.chunking.tokenize import ApproxCjkTokenCounter, TokenCounter
from fkh.domain.chunking.types import Chunk, ChunkingConfig, ChunkingResult, ChunkKind
from fkh.domain.parsing.types import BlockKind, Locator, ParsedDocument

_SENTENCE_BREAKS = "。！？；!?;"


class _Unit:
    """切片的最小单位：一段文本及其定位。

    heading_path 精确到所在小节（用于 child 的引用与检索过滤）；
    section_path 只保留到分组层级（用于决定 parent 边界）。
    """

    __slots__ = ("heading_path", "locator", "section_path", "table_index", "text", "tokens")

    def __init__(
        self,
        text: str,
        tokens: int,
        heading_path: tuple[str, ...],
        locator: Locator,
        section_path: tuple[str, ...],
        table_index: int | None = None,
    ) -> None:
        self.text = text
        self.tokens = tokens
        self.heading_path = heading_path
        self.section_path = section_path
        self.locator = locator
        self.table_index = table_index


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _merge_locators(first: Locator, last: Locator) -> Locator:
    return Locator(
        block_index=first.block_index,
        page_start=first.page_start if first.page_start is not None else last.page_start,
        page_end=last.page_end if last.page_end is not None else first.page_end,
        paragraph_index=first.paragraph_index,
        table_index=first.table_index,
        row_index=first.row_index,
        bbox=first.bbox,
        anchor=first.anchor,
    )


def _to_units(document: ParsedDocument, counter: TokenCounter, config: ChunkingConfig) -> list[_Unit]:
    units: list[_Unit] = []
    stack: list[tuple[int, str]] = []

    def paths() -> tuple[tuple[str, ...], tuple[str, ...]]:
        full = tuple(text for _, text in stack)
        section = tuple(text for level, text in stack if level <= config.section_break_level)
        return full, section

    for block in document.blocks:
        if block.kind is BlockKind.HEADING:
            level = block.level or 1
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, block.text))
            full_path, section_path = paths()
            units.append(
                _Unit(block.text, counter.count(block.text), full_path, block.locator, section_path)
            )
            continue

        full_path, section_path = paths()

        if block.table is not None and counter.count(block.text) > config.parent_max_tokens:
            # 大表按行拆，每行都带表头与单位
            for row_index, row_text in enumerate(block.table.iter_row_texts()):
                locator = Locator(
                    block_index=block.locator.block_index,
                    page_start=block.locator.page_start,
                    page_end=block.locator.page_end,
                    table_index=block.locator.table_index,
                    row_index=row_index,
                    bbox=block.locator.bbox,
                )
                units.append(
                    _Unit(
                        row_text,
                        counter.count(row_text),
                        full_path,
                        locator,
                        section_path,
                        block.locator.table_index,
                    )
                )
            continue

        units.append(
            _Unit(
                block.text,
                counter.count(block.text),
                full_path,
                block.locator,
                section_path,
                block.locator.table_index,
            )
        )

    return units


def _split_text(text: str, counter: TokenCounter, limit: int) -> list[str]:
    """把超长文本压到 limit 以内，优先在段落、句子边界切。"""
    if counter.count(text) <= limit:
        return [text]

    by_line = [line for line in text.split("\n") if line.strip()]
    if len(by_line) > 1:
        return _pack_texts(by_line, counter, limit)

    sentences: list[str] = []
    buffer = ""
    for char in text:
        buffer += char
        if char in _SENTENCE_BREAKS:
            sentences.append(buffer)
            buffer = ""
    if buffer:
        sentences.append(buffer)
    if len(sentences) > 1:
        return _pack_texts(sentences, counter, limit)

    return _hard_split(text, counter, limit)


def _pack_texts(pieces: Sequence[str], counter: TokenCounter, limit: int) -> list[str]:
    groups: list[str] = []
    current: list[str] = []
    total = 0
    for piece in pieces:
        tokens = counter.count(piece)
        if current and total + tokens > limit:
            groups.append("\n".join(current))
            current = []
            total = 0
        if tokens > limit:
            # 单片段本身超限（例如无标点的长串），只能硬切
            if current:
                groups.append("\n".join(current))
                current = []
                total = 0
            groups.extend(_hard_split(piece, counter, limit))
            continue
        current.append(piece)
        total += tokens
    if current:
        groups.append("\n".join(current))
    return groups


def _hard_split(text: str, counter: TokenCounter, limit: int) -> list[str]:
    chunks: list[str] = []
    start = 0
    length = len(text)
    while start < length:
        end = min(length, start + limit)
        while end > start + 1 and counter.count(text[start:end]) > limit:
            end -= max(1, (end - start) // 10)
        chunks.append(text[start:end])
        start = end
    return chunks


def _explode_oversized(units: Sequence[_Unit], counter: TokenCounter, limit: int) -> list[_Unit]:
    exploded: list[_Unit] = []
    for unit in units:
        if unit.tokens <= limit:
            exploded.append(unit)
            continue
        for piece in _split_text(unit.text, counter, limit):
            exploded.append(
                _Unit(
                    piece,
                    counter.count(piece),
                    unit.heading_path,
                    unit.locator,
                    unit.section_path,
                    unit.table_index,
                )
            )
    return exploded


def _group_sections(units: Sequence[_Unit]) -> list[list[_Unit]]:
    """按 section_path 分组，而不是按完整标题路径。

    完整路径变化就切分会让"### 0.1 / ### 0.2"这类密集小节各成一个 parent，
    最终 parent 只剩一两百 token，失去上下文价值。
    """
    sections: list[list[_Unit]] = []
    current: list[_Unit] = []
    current_section: tuple[str, ...] | None = None

    for unit in units:
        if current_section is None or unit.section_path == current_section:
            current.append(unit)
            current_section = unit.section_path
            continue
        if current:
            sections.append(current)
        current = [unit]
        current_section = unit.section_path

    if current:
        sections.append(current)
    return sections


def _pack(units: Sequence[_Unit], limit: int) -> list[list[_Unit]]:
    groups: list[list[_Unit]] = []
    current: list[_Unit] = []
    total = 0

    for unit in units:
        if current and total + unit.tokens > limit:
            groups.append(current)
            current = []
            total = 0
        current.append(unit)
        total += unit.tokens

    if current:
        groups.append(current)
    return groups


def build_chunks(
    document: ParsedDocument,
    *,
    counter: TokenCounter | None = None,
    config: ChunkingConfig | None = None,
) -> ChunkingResult:
    token_counter: TokenCounter = counter or ApproxCjkTokenCounter()
    chunk_config = config or ChunkingConfig()
    warnings: list[str] = []

    units = _to_units(document, token_counter, chunk_config)
    if not units:
        return ChunkingResult(
            parents=(),
            children=(),
            warnings=("文档无可切片内容",),
            tokenizer_id=token_counter.id,
        )

    sections = _group_sections(units)
    parent_groups: list[list[_Unit]] = []
    for section in sections:
        exploded = _explode_oversized(section, token_counter, chunk_config.parent_max_tokens)
        parent_groups.extend(_pack(exploded, chunk_config.parent_max_tokens))

    parents: list[Chunk] = []
    children: list[Chunk] = []

    for parent_units in parent_groups:
        parent_text = "\n\n".join(unit.text for unit in parent_units)
        parent_tokens = token_counter.count(parent_text)
        parent_ordinal = len(parents)

        if parent_tokens < chunk_config.parent_min_tokens:
            warnings.append(
                f"parent#{parent_ordinal} 仅 {parent_tokens} tokens，低于下限 "
                f"{chunk_config.parent_min_tokens}（标题分段造成，可接受但需抽查）"
            )

        parents.append(
            Chunk(
                kind=ChunkKind.PARENT,
                ordinal=parent_ordinal,
                text=parent_text,
                token_count=parent_tokens,
                # parent 可能跨越多个子标题，用分组层级路径更贴合它的实际边界；
                # 精确到小节的路径由 child 承担
                heading_path=parent_units[0].section_path or parent_units[0].heading_path,
                locator=_merge_locators(parent_units[0].locator, parent_units[-1].locator),
                content_sha256=_sha(parent_text),
                parent_ordinal=None,
                table_index=parent_units[0].table_index,
            )
        )

        child_units = _explode_oversized(parent_units, token_counter, chunk_config.child_max_tokens)
        for child_group in _pack(child_units, chunk_config.child_max_tokens):
            child_text = "\n\n".join(unit.text for unit in child_group)
            child_tokens = token_counter.count(child_text)
            if child_tokens < chunk_config.child_min_tokens:
                warnings.append(
                    f"child#{len(children)} 仅 {child_tokens} tokens，低于下限 "
                    f"{chunk_config.child_min_tokens}"
                )
            children.append(
                Chunk(
                    kind=ChunkKind.CHILD,
                    ordinal=len(children),
                    text=child_text,
                    token_count=child_tokens,
                    heading_path=child_group[0].heading_path,
                    locator=_merge_locators(child_group[0].locator, child_group[-1].locator),
                    content_sha256=_sha(child_text),
                    parent_ordinal=parent_ordinal,
                    table_index=child_group[0].table_index,
                )
            )

    return ChunkingResult(
        parents=tuple(parents),
        children=tuple(children),
        warnings=tuple(warnings),
        tokenizer_id=token_counter.id,
    )
