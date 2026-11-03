"""切片数据模型。

Parent / Child 的关系是检索质量与权限安全的交汇点：
child 用于精确召回，parent 用于给模型足够上下文；而 parent 的展开必须重新校验
ACL（见 fkh.authz.parent）。这里保存的 parent_ordinal 是两者在文档内的稳定关联。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from fkh.domain.parsing.types import Locator


class ChunkKind(StrEnum):
    PARENT = "parent"
    CHILD = "child"


@dataclass(frozen=True, slots=True)
class Chunk:
    kind: ChunkKind
    ordinal: int
    text: str
    token_count: int
    heading_path: tuple[str, ...]
    locator: Locator
    content_sha256: str
    parent_ordinal: int | None = None
    table_index: int | None = None


@dataclass(frozen=True, slots=True)
class ChunkingConfig:
    """初始试验值，来自 L1 §4.3。不是验收阈值，需在试点校准。

    section_break_level 决定多深的标题才切断 parent。
    取 2 表示按一级/二级标题分组，三级以下子标题只影响 child 的 heading_path。
    若按所有标题切分，标题密集的文档（如契约、SOP）会产生大量百 token 级碎片，
    使 parent 失去"提供完整上下文"的作用。
    """

    parent_min_tokens: int = 800
    parent_max_tokens: int = 1500
    child_min_tokens: int = 250
    child_max_tokens: int = 450
    section_break_level: int = 2

    def __post_init__(self) -> None:
        if self.parent_min_tokens > self.parent_max_tokens:
            raise ValueError("parent_min_tokens 不得大于 parent_max_tokens")
        if self.child_min_tokens > self.child_max_tokens:
            raise ValueError("child_min_tokens 不得大于 child_max_tokens")
        if self.child_max_tokens > self.parent_max_tokens:
            raise ValueError("child_max_tokens 不得大于 parent_max_tokens")
        if not 1 <= self.section_break_level <= 6:
            raise ValueError("section_break_level 必须在 1..6 之间")


@dataclass(frozen=True, slots=True)
class ChunkingResult:
    parents: tuple[Chunk, ...]
    children: tuple[Chunk, ...]
    warnings: tuple[str, ...] = ()
    tokenizer_id: str = "approx-cjk-v1"
