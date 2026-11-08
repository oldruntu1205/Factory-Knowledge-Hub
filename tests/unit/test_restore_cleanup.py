"""restore 失败清理：单个清理动作失败不得中断其余清理，也不得盖掉原始异常。"""

from __future__ import annotations

from pathlib import Path

import pytest

from fkh.tools import instance


def test_cleanup_continues_after_individual_failures(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    workdir = tmp_path / "restore"
    workdir.mkdir()
    pgdata = workdir / "pgdata"
    pgdata.mkdir()
    secrets = workdir / "secrets"
    secrets.mkdir()
    (workdir / "instance.json").write_text("{}", encoding="utf-8")
    # 让 workdir.rmdir 失败（目录非空），验证"保留并报告"而不是静默
    (workdir / "residual.bin").write_text("x", encoding="utf-8")

    calls: list[str] = []
    drop_calls: list[str] = []

    def fake_exec(dsn: str, sql: str) -> None:
        drop_calls.append(sql)
        if "DROP DATABASE" in sql:
            raise RuntimeError("drop database failed")

    def fake_stop(pgdata_path: Path) -> None:
        calls.append("stop")
        raise RuntimeError("stop failed")

    def fake_rmtree(path: Path) -> None:
        calls.append(f"rmtree:{path}")
        if "pgdata" in str(path):
            raise RuntimeError("rmtree failed")

    monkeypatch.setattr(instance, "_exec", fake_exec)
    monkeypatch.setattr(instance, "stop_postgres", fake_stop)
    monkeypatch.setattr(instance.shutil, "rmtree", fake_rmtree)

    failures = instance._cleanup_restore(
        workdir=workdir,
        pgdata=pgdata,
        root_dsn="postgresql://admin:pw@127.0.0.1:1/postgres",
        db_name="fkh_restore",
        role="fkh_app_restore",
        created_db=True,
        created_role=True,
        db_creation_attempted=True,
        role_creation_attempted=True,
        created_pgdata=True,
        created_secrets=True,
        instance_written=True,
        workdir_created=True,
    )

    labels = [failure.split(":")[0] for failure in failures]
    assert labels == [
        "drop database",
        "stop postgres",
        "remove pgdata",
        "remove workdir（非空则保留）",
    ]
    # 后续资源仍被尝试：drop role / remove secrets / instance.json 删除
    assert any("DROP ROLE" in sql for sql in drop_calls)
    assert not (workdir / "instance.json").exists()
    assert any("rmtree" in call and "secrets" in call for call in calls)
    assert workdir.exists()


def test_cleanup_reports_unconfirmed_creations_without_dropping(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    workdir = tmp_path / "restore"
    workdir.mkdir()
    calls: list[str] = []
    monkeypatch.setattr(instance, "_exec", lambda dsn, sql: calls.append(sql))

    failures = instance._cleanup_restore(
        workdir=workdir,
        pgdata=workdir / "pgdata",
        root_dsn="postgresql://admin:pw@127.0.0.1:1/postgres",
        db_name="fkh_unconfirmed",
        role="fkh_app_unconfirmed",
        created_db=False,
        created_role=False,
        db_creation_attempted=True,
        role_creation_attempted=True,
        created_pgdata=False,
        created_secrets=False,
        instance_written=False,
        workdir_created=False,
    )

    assert calls == [], "未确认归属的库/角色不得按名字删除"
    assert any("database fkh_unconfirmed" in item for item in failures)
    assert any("role fkh_app_unconfirmed" in item for item in failures)
    assert all("请人工核查" in item for item in failures)


def test_cleanup_ignores_missing_resources(tmp_path: Path) -> None:
    workdir = tmp_path / "restore"
    workdir.mkdir()
    failures = instance._cleanup_restore(
        workdir=workdir,
        pgdata=workdir / "pgdata",
        root_dsn=None,
        db_name="fkh_restore",
        role="fkh_app_restore",
        created_db=False,
        created_role=False,
        db_creation_attempted=False,
        role_creation_attempted=False,
        created_pgdata=False,
        created_secrets=False,
        instance_written=False,
        workdir_created=False,
    )
    assert failures == []
    assert workdir.exists()
