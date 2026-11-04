"""Embedding 设备/精度兼容性自检。

用途：在换显卡、换机器、改精度（fp32 ↔ fp16）前后各跑一次，判断两套配置产出的
向量能否混用。典型场景是"本地 3060 上做完索引，客户环境换成 T4 继续增量入库"。

它回答的问题很具体：同一批文本，配置 A 与配置 B 编码出的向量，在**检索排序**上
是否等价。等价就意味着已有索引不需要重建。

用法：

    # 同一模型，比较 GPU 与 CPU
    uv run python -m fkh.tools.embedding_parity --texts samples.txt \\
        --a-provider bge-m3 --a-device cuda --b-provider bge-m3 --b-device cpu

    # 不装模型时的链路自检（伪向量，只验证工具本身与流程）
    uv run python -m fkh.tools.embedding_parity --texts samples.txt \\
        --a-provider test --b-provider test

判定阈值：
- 同文本余弦均值 ≥ 0.999：向量本身几乎一致；
- top-k 检索重叠率 ≥ 0.95：排序结果不因换设备而改变。

两项都过才建议直接混用；只过一项，建议在同一环境重跑一次索引再上。
"""

from __future__ import annotations

import argparse
import math
import sys
from collections.abc import Sequence
from pathlib import Path

from fkh.retrieval.embedding import Embedder, build_embedder

DEFAULT_TOP_K = 10
MIN_COSINE = 0.999
MIN_OVERLAP = 0.95


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _ranking(vectors: Sequence[Sequence[float]], index: int, top_k: int) -> list[int]:
    """以第 index 条为查询，返回其余文本的相似度排名。"""
    query = vectors[index]
    scored = [(i, _cosine(query, vec)) for i, vec in enumerate(vectors) if i != index]
    scored.sort(key=lambda item: (-item[1], item[0]))
    return [i for i, _ in scored[:top_k]]


def compare(
    texts: Sequence[str],
    embedder_a: Embedder,
    embedder_b: Embedder,
    *,
    top_k: int = DEFAULT_TOP_K,
) -> dict[str, float]:
    vectors_a = embedder_a.embed_documents(texts)
    vectors_b = embedder_b.embed_documents(texts)

    if len(vectors_a) != len(texts) or len(vectors_b) != len(texts):
        raise RuntimeError("向量数量与文本数量不一致")

    same_text = [_cosine(va, vb) for va, vb in zip(vectors_a, vectors_b, strict=True)]
    mean_cosine = sum(same_text) / len(same_text)
    min_cosine = min(same_text)

    overlaps: list[float] = []
    for index in range(len(texts)):
        rank_a = set(_ranking(vectors_a, index, top_k))
        rank_b = set(_ranking(vectors_b, index, top_k))
        if not rank_a and not rank_b:
            continue
        union = rank_a | rank_b
        overlaps.append(len(rank_a & rank_b) / len(union) if union else 1.0)

    mean_overlap = sum(overlaps) / len(overlaps) if overlaps else 1.0
    return {
        "vectors": float(len(texts)),
        "mean_cosine": mean_cosine,
        "min_cosine": min_cosine,
        "mean_overlap": mean_overlap,
    }


def _load_texts(path: Path) -> list[str]:
    lines = [line.strip() for line in path.read_text(encoding="utf-8").splitlines()]
    texts = [line for line in lines if line and not line.startswith("#")]
    if len(texts) < 3:
        raise SystemExit("样本太少：至少需要 3 条非空文本才能比较排序")
    return texts


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Embedding 设备/精度兼容性自检")
    parser.add_argument("--texts", required=True, type=Path, help="每行一条文本的样本文本")
    parser.add_argument("--a-provider", default="test", help="配置 A 的 provider")
    parser.add_argument("--a-device", default="cpu", help="配置 A 的设备")
    parser.add_argument("--b-provider", default="test", help="配置 B 的 provider")
    parser.add_argument("--b-device", default="cpu", help="配置 B 的设备")
    parser.add_argument("--top-k", type=int, default=DEFAULT_TOP_K)
    args = parser.parse_args(argv)

    texts = _load_texts(args.texts)

    embedder_a = build_embedder(args.a_provider, device=args.a_device)
    embedder_b = build_embedder(args.b_provider, device=args.b_device)
    if embedder_a is None or embedder_b is None:
        print("provider=none 没有向量可比，请指定实际模型或 test", file=sys.stderr)
        return 2
    if embedder_a.dimension != embedder_b.dimension:
        print(
            f"维度不同（{embedder_a.dimension} vs {embedder_b.dimension}），无法混用，"
            "必须重建索引",
            file=sys.stderr,
        )
        return 2

    stats = compare(texts, embedder_a, embedder_b, top_k=args.top_k)
    print(f"样本数：{int(stats['vectors'])}")
    print(f"同文本余弦：均值 {stats['mean_cosine']:.6f}，最小 {stats['min_cosine']:.6f}")
    print(f"top-{args.top_k} 排序重叠：{stats['mean_overlap']:.4f}")

    cosine_ok = stats["min_cosine"] >= MIN_COSINE
    overlap_ok = stats["mean_overlap"] >= MIN_OVERLAP
    if cosine_ok and overlap_ok:
        print("结论：可混用，已有索引无需重建")
        return 0
    if cosine_ok or overlap_ok:
        print("结论：存在偏差，建议在原环境重跑索引后再切换")
        return 1
    print("结论：差异显著，不要混用", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
