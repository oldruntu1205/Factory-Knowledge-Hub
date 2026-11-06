"""检索增强生成（RAG）的本地推理层。

对外只暴露 `TextGenerator` 与两个错误类型；答案编排在 `fkh.answer`。
"""

from __future__ import annotations

from fkh.generation.client import (
    DeterministicGenerator,
    GenerationError,
    GenerationNotConfigured,
    GenerationResult,
    OllamaGenerator,
    TextGenerator,
    build_generator,
    get_generator,
)

__all__ = [
    "DeterministicGenerator",
    "GenerationError",
    "GenerationNotConfigured",
    "GenerationResult",
    "OllamaGenerator",
    "TextGenerator",
    "build_generator",
    "get_generator",
]
