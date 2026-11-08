"""restore 前置条件与失败清理的回归（不启动内嵌 PG 的部分）。"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from fkh.tools import instance


def _args(tmp_path: Path, dump: Path, workdir: Path) -> argparse.Namespace:
    return argparse.Namespace(
        from_dump=str(dump),
        workdir=str(workdir),
        db_port=55991,
        api_port=8191,
        db_name="fkh_restore_probe",
        name="restore-probe",
    )


def test_restore_refuses_non_empty_target_and_keeps_it(tmp_path: Path) -> None:
    dump = tmp_path / "backup.dump"
    dump.write_bytes(b"not-a-real-dump")
    target = tmp_path / "target"
    target.mkdir()
    sentinel = target / "sentinel.txt"
    sentinel.write_text("keep-me", encoding="utf-8")

    with pytest.raises(SystemExit, match="全新目录"):
        instance.command_restore(_args(tmp_path, dump, target))

    assert sentinel.read_text(encoding="utf-8") == "keep-me"
    assert not (target / "instance.json").exists()
    assert not (target / "secrets").exists()
    assert not (target / "pgdata").exists()


def test_restore_refuses_non_directory_target(tmp_path: Path) -> None:
    dump = tmp_path / "backup.dump"
    dump.write_bytes(b"not-a-real-dump")
    target = tmp_path / "target"
    target.write_text("i am a file", encoding="utf-8")

    with pytest.raises(SystemExit, match="不是目录"):
        instance.command_restore(_args(tmp_path, dump, target))
    assert target.read_text(encoding="utf-8") == "i am a file"


def test_restore_bad_dump_leaves_pre_existing_empty_dir_empty(tmp_path: Path) -> None:
    dump = tmp_path / "backup.dump"
    dump.write_bytes(b"not-a-real-dump")
    target = tmp_path / "target"
    target.mkdir()

    with pytest.raises(SystemExit, match="备份文件不可读"):
        instance.command_restore(_args(tmp_path, dump, target))

    assert target.exists()
    assert list(target.iterdir()) == []


def test_restore_missing_dump_creates_nothing(tmp_path: Path) -> None:
    target = tmp_path / "target"

    with pytest.raises(SystemExit, match="备份文件不存在"):
        instance.command_restore(_args(tmp_path, tmp_path / "missing.dump", target))

    assert not target.exists()
