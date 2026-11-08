"""实例启停身份判定与自定义域重启探测的回归（不连库、不起进程）。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import psutil
import pytest

from fkh.tools import instance

DB_PORT = 55495
DB_NAME = "fkh_test"
DB_URL = f"postgresql://app:pw@127.0.0.1:{DB_PORT}/{DB_NAME}"


def _env(nonce: str | None = None) -> dict[str, str]:
    env = {"FKH_DATABASE_URL": DB_URL}
    if nonce is not None:
        env["FKH_INSTANCE_PID_NONCE"] = nonce
    return env


class _FakeProc:
    def __init__(
        self,
        pid: int,
        args: list[str],
        *,
        env: dict[str, str] | None = None,
        parents: list[int] | None = None,
    ) -> None:
        self.pid = pid
        self._args = args
        self._env = env
        self._parents = parents or []

    def is_running(self) -> bool:
        return True

    def create_time(self) -> float:
        return 1000.0

    def cmdline(self) -> list[str]:
        return self._args

    def environ(self) -> dict[str, str]:
        if self._env is None:
            raise psutil.AccessDenied(self.pid)
        return self._env

    def parents(self) -> list[_FakeProc]:
        return [_FakeProc(pid, []) for pid in self._parents]


def _install_procs(monkeypatch: pytest.MonkeyPatch, procs: dict[int, _FakeProc]) -> None:
    import psutil

    def factory(pid: int) -> _FakeProc:
        if pid not in procs:
            raise psutil.NoSuchProcess(pid)
        return procs[pid]

    monkeypatch.setattr(psutil, "Process", factory)


def test_classify_accepts_nonce_match(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_procs(
        monkeypatch,
        {10: _FakeProc(10, ["python", "-m", "uvicorn", "fkh.api.main:app", "--port", "8151"],
                       env=_env("n-1"))},
    )
    verdict = instance._classify_api_process(
        10, api_port=8151, db_port=DB_PORT, db_name=DB_NAME, nonce="n-1"
    )
    assert verdict.state == "ours"
    assert verdict.create_time is not None


def test_classify_rejects_other_service_even_if_uvicorn(monkeypatch: pytest.MonkeyPatch) -> None:
    # 另一个 uvicorn 服务：命令行含 uvicorn，但不是本 app / 不是本端口
    _install_procs(
        monkeypatch,
        {20: _FakeProc(20, ["python", "-m", "uvicorn", "foreign_app:app", "--port", "8199"],
                       env=_env())},
    )
    verdict = instance._classify_api_process(
        20, api_port=8151, db_port=DB_PORT, db_name=DB_NAME
    )
    assert verdict.state == "foreign"
    assert "foreign_app" in verdict.cmdline


def test_classify_rejects_own_app_on_other_port(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_procs(
        monkeypatch,
        {30: _FakeProc(30, ["python", "-m", "uvicorn", "fkh.api.main:app", "--port", "8199"],
                       env=_env())},
    )
    monkeypatch.setattr(instance, "port_owner", lambda port: {"pid": 999, "name": "other"})
    verdict = instance._classify_api_process(
        30, api_port=8151, db_port=DB_PORT, db_name=DB_NAME
    )
    assert verdict.state == "foreign"


def test_classify_rejects_nonce_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_procs(
        monkeypatch,
        {40: _FakeProc(40, ["python", "-m", "uvicorn", "fkh.api.main:app", "--port", "8151"],
                       env=_env("other-nonce"))},
    )
    verdict = instance._classify_api_process(
        40, api_port=8151, db_port=DB_PORT, db_name=DB_NAME, nonce="expected"
    )
    assert verdict.state == "foreign"


def test_classify_denied_when_env_unreadable(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_procs(
        monkeypatch,
        {41: _FakeProc(41, ["python", "-m", "uvicorn", "fkh.api.main:app", "--port", "8151"])},
    )
    verdict = instance._classify_api_process(
        41, api_port=8151, db_port=DB_PORT, db_name=DB_NAME, nonce="expected"
    )
    assert verdict.state == "denied"


def test_classify_accepts_listener_child_of_launcher(monkeypatch: pytest.MonkeyPatch) -> None:
    launcher = _FakeProc(
        100, ["python", "-m", "uvicorn", "fkh.api.main:app", "--port", "8151"], env=_env()
    )
    listener = _FakeProc(
        101,
        ["python", "-m", "uvicorn", "fkh.api.main:app", "--port", "8151"],
        env=_env(),
        parents=[100],
    )
    _install_procs(monkeypatch, {100: launcher, 101: listener})
    monkeypatch.setattr(instance, "port_owner", lambda port: {"pid": 101, "name": "python.exe"})
    verdict = instance._classify_api_process(
        100, api_port=8151, db_port=DB_PORT, db_name=DB_NAME
    )
    assert verdict.state == "ours"


def test_classify_rejects_wrong_database(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_procs(
        monkeypatch,
        {50: _FakeProc(
            50,
            ["python", "-m", "uvicorn", "fkh.api.main:app", "--port", "8151"],
            env={"FKH_DATABASE_URL": "postgresql://app:pw@127.0.0.1:55495/other_db"},
        )},
    )
    verdict = instance._classify_api_process(
        50, api_port=8151, db_port=DB_PORT, db_name=DB_NAME
    )
    assert verdict.state == "foreign"


def test_classify_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_procs(monkeypatch, {})
    verdict = instance._classify_api_process(
        404, api_port=8151, db_port=DB_PORT, db_name=DB_NAME
    )
    assert verdict.state == "absent"


def test_read_pid_file_supports_legacy_and_json(tmp_path: Path) -> None:
    legacy = tmp_path / "legacy.pid"
    legacy.write_text("1234", encoding="utf-8")
    assert instance._read_pid_file(legacy) == {
        "pid": 1234, "nonce": None, "launcher_pid": None, "workdir": "",
    }

    modern = tmp_path / "modern.pid"
    modern.write_text(
        json.dumps({"pid": 5678, "nonce": "n", "launcher_pid": 5677,
                    "workdir": str(tmp_path)}),
        encoding="utf-8",
    )
    assert instance._read_pid_file(modern) == {
        "pid": 5678, "nonce": "n", "launcher_pid": 5677, "workdir": str(tmp_path),
    }


def test_read_pid_file_rejects_garbage(tmp_path: Path) -> None:
    bad = tmp_path / "bad.pid"
    bad.write_text("not-a-pid", encoding="utf-8")
    with pytest.raises(SystemExit):
        instance._read_pid_file(bad)


def test_domains_cover_allows_registered_custom_domain() -> None:
    assert instance._domains_cover(
        ["product_catalog", "quality"], {"product_catalog", "quality", "custom_field"}
    )


def test_domains_cover_still_requires_initial_domains() -> None:
    assert not instance._domains_cover(["product_catalog"], {"quality"})


def test_stop_refuses_foreign_pid_without_terminating(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    workdir = tmp_path / "inst"
    workdir.mkdir()
    (workdir / "api.pid").write_text("1234", encoding="utf-8")
    record: dict[str, Any] = {
        "api_port": 8151,
        "db_port": DB_PORT,
        "db_name": DB_NAME,
        "pgdata": str(workdir / "pgdata"),
    }
    _install_procs(
        monkeypatch,
        {1234: _FakeProc(1234, ["python", "-m", "http.server", "8000"])},
    )
    monkeypatch.setattr(instance, "load_instance", lambda path: record)
    monkeypatch.setattr(instance, "port_owner", lambda port: {"pid": 999, "name": "other"})
    terminated: list[int] = []
    monkeypatch.setattr(instance, "_terminate_process",
                        lambda verdict: terminated.append(verdict.pid))
    monkeypatch.setattr(instance, "stop_postgres", lambda pgdata: terminated.append(-1))

    with pytest.raises(SystemExit) as excinfo:
        instance.command_stop(argparse.Namespace(workdir=str(workdir)))

    assert "拒绝停止" in str(excinfo.value)
    assert terminated == []
    assert (workdir / "api.pid").exists()


def test_terminate_refuses_foreign_verdict() -> None:
    with pytest.raises(SystemExit, match="未被证明"):
        instance._terminate_process(instance._ProcessVerdict(1, "foreign", ""))


def test_terminate_refuses_reused_pid(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Reused:
        def create_time(self) -> float:
            return 2000.0

    monkeypatch.setattr(psutil, "Process", lambda pid: _Reused())
    with pytest.raises(SystemExit, match="已被替换"):
        instance._terminate_process(instance._ProcessVerdict(1, "ours", "", 1000.0))


def test_stop_treats_absent_pid_as_stale_and_stops_pg(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workdir = tmp_path / "inst"
    workdir.mkdir()
    (workdir / "api.pid").write_text("1234", encoding="utf-8")
    record: dict[str, Any] = {
        "api_port": 8151,
        "db_port": DB_PORT,
        "db_name": DB_NAME,
        "pgdata": str(workdir / "pgdata"),
    }
    _install_procs(monkeypatch, {})
    monkeypatch.setattr(instance, "load_instance", lambda path: record)
    monkeypatch.setattr(instance, "port_owner", lambda port: None)
    calls: list[str] = []
    monkeypatch.setattr(instance, "stop_postgres", lambda pgdata: calls.append("pg"))
    monkeypatch.setattr(instance, "postgres_status", lambda pgdata: False)

    assert instance.command_stop(argparse.Namespace(workdir=str(workdir))) == 0
    assert calls == ["pg"]
    assert not (workdir / "api.pid").exists()
    assert '"stopped": true' in capsys.readouterr().out


def test_stop_prefers_listener_before_launcher(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    workdir = tmp_path / "inst"
    workdir.mkdir()
    (workdir / "api.pid").write_text(
        json.dumps({"pid": 101, "launcher_pid": 100, "nonce": "n",
                    "workdir": str(workdir)}),
        encoding="utf-8",
    )
    record: dict[str, Any] = {
        "api_port": 8151,
        "db_port": DB_PORT,
        "db_name": DB_NAME,
        "pgdata": str(workdir / "pgdata"),
    }
    launcher = _FakeProc(
        100, ["python", "-m", "uvicorn", "fkh.api.main:app", "--port", "8151"],
        env=_env("n"),
    )
    listener = _FakeProc(
        101, ["python", "-m", "uvicorn", "fkh.api.main:app", "--port", "8151"],
        env=_env("n"), parents=[100],
    )
    _install_procs(monkeypatch, {100: launcher, 101: listener})
    monkeypatch.setattr(instance, "load_instance", lambda path: record)
    listening = True

    def port_owner(port: int) -> dict[str, Any] | None:
        return {"pid": 101, "name": "python.exe"} if listening else None

    order: list[int] = []

    def terminate(verdict: Any) -> None:
        nonlocal listening
        order.append(verdict.pid)
        listening = False

    monkeypatch.setattr(instance, "port_owner", port_owner)
    monkeypatch.setattr(instance, "_terminate_process", terminate)
    monkeypatch.setattr(instance, "stop_postgres", lambda pgdata: order.append(-1))
    monkeypatch.setattr(instance, "postgres_status", lambda pgdata: False)

    assert instance.command_stop(argparse.Namespace(workdir=str(workdir))) == 0
    assert order[:2] == [101, 100]
