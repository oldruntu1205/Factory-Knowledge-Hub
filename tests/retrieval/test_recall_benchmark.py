"""Recall@K 评测工具的口径测试（纯逻辑，不下载模型、不连库）。

评测本身可以出错，但「命中怎么算、汇总怎么算」必须是确定性的；
否则一个口径 bug 会把 recall 算高，直接误导是否启用混合模式的决定。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fkh.tools.recall_benchmark import BenchmarkCase, hit_at_k, load_cases, summarize

ROOT = Path(__file__).resolve().parents[2]


def test_hit_at_k_only_counts_top_k() -> None:
    assert hit_at_k(["无关", "目标片段在这里"], "目标", 2)
    assert not hit_at_k(["无关", "目标片段在这里"], "目标", 1)


def test_hit_at_k_rejects_non_positive_k() -> None:
    # k<=0 不能被当成「空结果也算命中」
    assert not hit_at_k([], "目标", 0)
    assert not hit_at_k(["目标"], "目标", -1)


def test_hit_at_k_requires_exact_substring() -> None:
    assert not hit_at_k(["目标的近义词"], "目标片段", 5)


def test_summarize_reports_delta_and_only_hits() -> None:
    rows = [
        {"id": "1", "lexical_hit": True, "hybrid_hit": True, "lexical_ms": 10, "hybrid_ms": 20},
        {"id": "2", "lexical_hit": False, "hybrid_hit": True, "lexical_ms": 10, "hybrid_ms": 30},
        {"id": "3", "lexical_hit": True, "hybrid_hit": False, "lexical_ms": 20, "hybrid_ms": 10},
        {"id": "4", "lexical_hit": False, "hybrid_hit": False, "lexical_ms": 30, "hybrid_ms": 40},
    ]
    summary = summarize(rows)
    assert summary["cases"] == 4
    assert summary["lexical_recall"] == 0.5
    assert summary["hybrid_recall"] == 0.5
    assert summary["delta"] == 0.0
    assert summary["lexical_only_hits"] == 1
    assert summary["hybrid_only_hits"] == 1
    assert summary["avg_hybrid_ms"] == 25.0


def test_summarize_rejects_empty_rows() -> None:
    with pytest.raises(ValueError):
        summarize([])


def test_load_cases_reads_shipped_set() -> None:
    cases = load_cases(ROOT / "_evidence" / "recall_cases_20261104.json")
    assert len(cases) >= 50
    assert all(isinstance(case, BenchmarkCase) and case.query and case.needle for case in cases)


def test_load_cases_rejects_empty_set(tmp_path: Path) -> None:
    path = tmp_path / "empty.json"
    path.write_text(json.dumps({"cases": []}), encoding="utf-8")
    with pytest.raises(ValueError):
        load_cases(path)


def test_summarize_adds_document_level_recall_when_available() -> None:
    """切片边界会切断答案串；文档级口径用于区分「没找回」与「切断了」。"""
    rows = [
        {
            "id": "1",
            "lexical_hit": False,
            "hybrid_hit": True,
            "lexical_doc_hit": True,
            "hybrid_doc_hit": True,
            "lexical_ms": 1,
            "hybrid_ms": 2,
        },
        {
            "id": "2",
            "lexical_hit": False,
            "hybrid_hit": False,
            "lexical_doc_hit": False,
            "hybrid_doc_hit": True,
            "lexical_ms": 1,
            "hybrid_ms": 2,
        },
    ]
    summary = summarize(rows)
    assert summary["lexical_recall"] == 0.0
    assert summary["hybrid_recall"] == 0.5
    assert summary["lexical_doc_recall"] == 0.5
    assert summary["hybrid_doc_recall"] == 1.0
