"""密钥轮换的失败语义：连接中断不确定 vs 服务端明确拒绝。"""

from __future__ import annotations

from pathlib import Path

import psycopg
import pytest

from fkh.tools import instance


class _FakeConn:
    def __init__(self, exc: BaseException | None = None) -> None:
        self._exc = exc
        self.committed = False

    def __enter__(self) -> _FakeConn:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def execute(self, sql: str) -> None:
        if self._exc is not None:
            raise self._exc

    def commit(self) -> None:
        self.committed = True


def test_connection_loss_is_uncertain(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeConn(psycopg.OperationalError("connection lost"))
    monkeypatch.setattr(instance.psycopg, "connect", lambda *args, **kwargs: fake)

    with pytest.raises(instance._PasswordChangeUncertain):
        instance._alter_role_password("postgresql://admin@127.0.0.1/db", "app", "new")


def test_server_rejection_is_confirmed(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeConn(psycopg.errors.DatabaseError("permission denied"))
    monkeypatch.setattr(instance.psycopg, "connect", lambda *args, **kwargs: fake)

    with pytest.raises(SystemExit, match="被 PostgreSQL 拒绝"):
        instance._alter_role_password("postgresql://admin@127.0.0.1/db", "app", "new")
    assert fake.committed is False


def test_pending_secret_file_blocks_rotation(tmp_path: Path) -> None:
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "db-app.password.new").write_text("pending", encoding="utf-8")

    with pytest.raises(SystemExit, match="待恢复"):
        instance._assert_no_pending_secret_files(tmp_path)


def test_atomic_write_keeps_new_file_on_replace_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "jwt.secret"
    target.write_text("old\n", encoding="utf-8")

    def fail_replace(source: Path, destination: Path) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr(instance.os, "replace", fail_replace)
    with pytest.raises(SystemExit, match="替换"):
        instance._atomic_write_secret(target, "new")

    assert target.read_text(encoding="utf-8") == "old\n"
    assert (tmp_path / "jwt.secret.new").read_text(encoding="utf-8") == "new\n"
