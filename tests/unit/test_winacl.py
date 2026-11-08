"""Windows DACL 私有保护的回归（普通账户读不到的底层保证）。"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from fkh.tools import instance, winacl

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows ACL only")


def test_tighten_directory_and_file_only_allows_owner(tmp_path: Path) -> None:
    secret_dir = tmp_path / "secrets"
    secret_dir.mkdir()
    winacl.tighten(secret_dir, directory=True)
    secret = secret_dir / "jwt.secret"
    secret.write_text("x" * 48, encoding="utf-8")
    winacl.tighten(secret, directory=False)

    info = winacl.describe(secret)
    assert info["protected"] is True
    owner = winacl.current_user_sid()
    assert info["aces"] == [
        {"sid": owner, "allowed": True, "inherited": False, "mask": winacl.FILE_ALL_ACCESS}
    ]
    winacl.assert_private(secret_dir, directory=True)
    winacl.assert_private(secret, directory=False)


def test_assert_private_rejects_users_read_ace(tmp_path: Path) -> None:
    plain = tmp_path / "plain.txt"
    plain.write_text("not a secret", encoding="utf-8")
    # 显式给 BUILTIN\Users 读权限，模拟"普通账户可读"的默认继承结果
    subprocess.run(
        ["icacls", str(plain), "/grant", "*S-1-5-32-545:R"],
        check=True,
        capture_output=True,
    )
    with pytest.raises(winacl.AclError):
        winacl.assert_private(plain, directory=False)

    winacl.tighten(plain, directory=False)
    winacl.assert_private(plain, directory=False)


def test_assert_private_rejects_foreign_owner(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    current = winacl.current_user_sid()
    info = winacl.AclInfo(
        protected=True,
        owner_sid="S-1-5-21-9999999999-9999999999-9999999999-1234",
        aces=(winacl.Ace(current, True, False, winacl.FILE_ALL_ACCESS),),
    )
    monkeypatch.setattr(winacl, "read_acl", lambda path: info)
    with pytest.raises(winacl.AclError, match="所有者"):
        winacl.assert_private(tmp_path / "x", directory=False)


def test_write_secret_hardens_parent_and_file(tmp_path: Path) -> None:
    path = tmp_path / "secrets" / "db-admin.password"
    instance._write_secret(path, "s" * 48)

    assert path.read_text(encoding="utf-8").strip() == "s" * 48
    winacl.assert_private(path.parent, directory=True)
    winacl.assert_private(path, directory=False)
    assert all(
        ace["sid"] == winacl.current_user_sid() for ace in winacl.describe(path)["aces"]
    )
