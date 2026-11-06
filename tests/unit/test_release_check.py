"""上线自检工具的纯函数单测（不连库）。"""

from __future__ import annotations

from fkh.tools.release_check import Check, render


def test_render_reports_go_when_no_failure() -> None:
    text = render(
        [
            Check("数据库可达", "PASS", "ok"),
            Check("生成能力", "WARN", "provider=none"),
        ]
    )
    assert "结论：1 PASS / 1 WARN / 0 FAIL" in text
    assert text.endswith("GO")


def test_render_reports_no_go_on_failure() -> None:
    text = render(
        [
            Check("数据库可达", "PASS", "ok"),
            Check("RLS knowledge_chunks", "FAIL", "enable=True force=False"),
        ]
    )
    assert "1 PASS / 0 WARN / 1 FAIL" in text
    assert text.endswith("NO-GO")


def test_render_aligns_names() -> None:
    text = render([Check("短", "PASS", "x"), Check("一个很长的检查项名字", "PASS", "y")])
    lines = [line for line in text.splitlines() if line.startswith(("短", "一个很长"))]
    assert lines[0].index("PASS") == lines[1].index("PASS")
