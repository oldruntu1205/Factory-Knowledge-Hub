"""迁移执行器的跳号与回滚顺序（纯逻辑，不连库）。

背景：0011 跳过了预留给草稿终止的 0010。跳号本身允许，但会带出两件事：
1. 回滚不能按版本号倒序，必须按**应用顺序**倒序——否则预留号一旦晚落地，
   回滚顺序就与依赖顺序不一致；
2. 不允许把新迁移"补"进已应用版本之前（历史回填），否则应用顺序与版本顺序分叉。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _apply_module() -> Any:
    spec = importlib.util.spec_from_file_location("fkh_apply_under_test", ROOT / "migrations" / "apply.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def apply_module() -> Any:
    return _apply_module()


def _rows(*versions: str) -> list[tuple[str, str, int]]:
    """构造 (version, checksum, applied_at) —— applied_at 递增表示应用顺序。"""
    return [(version, "checksum", index) for index, version in enumerate(versions, start=1)]


def test_plan_rollback_uses_application_order_not_version_order(apply_module: Any) -> None:
    # 模拟预留号 0010 晚于 0011 落地：应用顺序 = 0011 先、0010 后
    rows = _rows("0009", "0011", "0010")
    assert apply_module.plan_rollback(rows, 1) == ["0010"]
    assert apply_module.plan_rollback(rows, 2) == ["0010", "0011"]
    # 若按版本号倒序会先回滚 0011——顺序与依赖相反，正是本次要修的点


def test_plan_rollback_skips_gap_without_failing(apply_module: Any) -> None:
    rows = _rows("0000", "0001", "0008", "0009", "0011")
    assert apply_module.plan_rollback(rows, 2) == ["0011", "0009"]


def test_plan_rollback_rejects_non_positive_steps(apply_module: Any) -> None:
    with pytest.raises(SystemExit):
        apply_module.plan_rollback(_rows("0011"), 0)


def test_missing_rollback_scripts_are_reported(apply_module: Any) -> None:
    assert apply_module.missing_rollback_scripts(["0011"]) == []
    assert apply_module.missing_rollback_scripts(["9999"]) == ["9999_down.sql"]


def test_backfill_of_lower_version_is_rejected(apply_module: Any) -> None:
    files = [
        ROOT / "migrations" / "0009_chunk_lexeme_tokenizer.sql",
        ROOT / "migrations" / "0010_future.sql",
    ]
    with pytest.raises(SystemExit) as excinfo:
        apply_module.assert_no_backfill(files, {"0011": "checksum"})
    assert "0010_future.sql" in str(excinfo.value)


def test_gap_without_backfill_is_allowed(apply_module: Any) -> None:
    files = [ROOT / "migrations" / "0011_retrieval_mode.sql"]
    apply_module.assert_no_backfill(files, {"0009": "checksum"})
