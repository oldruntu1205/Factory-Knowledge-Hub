"""语料目录收集：客户语料常见的 PDF/DOCX/HTML 必须能被评测工具收进来。"""

from __future__ import annotations

from pathlib import Path

import pytest

from fkh.tools.recall_benchmark import _collect_files


def test_collect_files_supports_parser_formats(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text("# a\n", encoding="utf-8")
    (tmp_path / "b.pdf").write_bytes(b"%PDF-1.4 fake")
    (tmp_path / "c.docx").write_bytes(b"PK fake")
    (tmp_path / "d.txt").write_text("not supported", encoding="utf-8")
    (tmp_path / "e.xlsx").write_bytes(b"PK fake")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "f.html").write_text("<h1>f</h1>", encoding="utf-8")

    files = _collect_files([str(tmp_path)])
    names = {path.name for path in files}
    assert names == {"a.md", "b.pdf", "c.docx", "f.html"}


def test_collect_files_rejects_explicit_unsupported_file(tmp_path: Path) -> None:
    bad = tmp_path / "notes.txt"
    bad.write_text("plain text", encoding="utf-8")

    with pytest.raises(ValueError, match="不支持的语料格式"):
        _collect_files([str(bad)])


def test_collect_files_accepts_explicit_supported_file(tmp_path: Path) -> None:
    good = tmp_path / "manual.pdf"
    good.write_bytes(b"%PDF-1.4 fake")
    assert _collect_files([str(good)]) == [good]
