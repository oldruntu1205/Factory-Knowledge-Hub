"""Embedding 兼容性自检工具的单测。

工具本身不复杂，但它决定了"换设备要不要重建索引"这个决策，输出不能错。
"""

from __future__ import annotations

import math

import pytest

from fkh.retrieval.embedding import DeterministicTestEmbedder
from fkh.tools import embedding_parity as parity


class _FixedEmbedder:
    """固定维度、固定向量的假 embedder，用于构造维度不一致的场景。"""

    def __init__(self, dimension: int) -> None:
        self.dimension = dimension
        self.model_id = f"fixed-{dimension}"

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [[0.1] * self.dimension for _ in texts]

    def embed_query(self, text: str) -> list[float]:
        return [0.1] * self.dimension


def test_identical_embedders_are_fully_equivalent() -> None:
    texts = ["冷凝管 24/29", "圆底烧瓶 250 mL", "磨口尺寸需匹配"]
    embedder = DeterministicTestEmbedder()
    stats = parity.compare(texts, embedder, embedder)

    assert stats["mean_cosine"] == pytest.approx(1.0, abs=1e-9)
    assert stats["min_cosine"] == pytest.approx(1.0, abs=1e-9)
    assert stats["mean_overlap"] == pytest.approx(1.0)


def test_different_embedders_show_divergence() -> None:
    texts = ["冷凝管", "烧瓶", "量筒", "漏斗"]
    stats = parity.compare(texts, DeterministicTestEmbedder(), _FixedEmbedder(1024))
    # 常量向量与哈希向量不相关，相似度必然明显低于阈值
    assert stats["mean_cosine"] < parity.MIN_COSINE


def test_cosine_handles_orthogonal_and_identical() -> None:
    assert parity._cosine([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
    assert parity._cosine([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)
    assert parity._cosine([0.0, 0.0], [1.0, 0.0]) == 0.0


def test_dimension_mismatch_fails_fast(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """维度不同就没有混用的可能，工具必须直接拒绝而不是给出一个看似正常的分数。"""
    sample = tmp_path / "samples.txt"
    sample.write_text("冷凝管\n烧瓶\n量筒\n", encoding="utf-8")

    sequence = iter([_FixedEmbedder(8), _FixedEmbedder(16)])

    def fake_build(provider: str, *, device: str = "cpu") -> _FixedEmbedder:
        return next(sequence)

    monkeypatch.setattr(parity, "build_embedder", fake_build)
    assert parity.main(["--texts", str(sample)]) == 2


def test_too_few_samples_rejected(tmp_path) -> None:
    sample = tmp_path / "few.txt"
    sample.write_text("只有一条\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        parity._load_texts(sample)


def test_comments_and_blank_lines_ignored(tmp_path) -> None:
    sample = tmp_path / "c.txt"
    sample.write_text("# 注释\n\n冷凝管\n烧瓶\n量筒\n", encoding="utf-8")
    assert parity._load_texts(sample) == ["冷凝管", "烧瓶", "量筒"]


def test_thresholds_are_documented_values() -> None:
    # 阈值是判定结论的依据，改动必须有意识
    assert math.isclose(parity.MIN_COSINE, 0.999)
    assert math.isclose(parity.MIN_OVERLAP, 0.95)
