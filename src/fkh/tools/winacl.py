"""Windows NTFS 私有访问控制（交付密钥的静态保护）。

为什么需要这个模块：Windows 上 `os.chmod(path, 0o600)` 只切换只读属性，
**不会改变 DACL**；默认继承的 `BUILTIN\\Users:(RX)` 仍然允许普通账户读取。
交付实例的 `secrets/` 必须显式设置「保护 DACL + 仅当前账户」，否则"0600"
只是给人看的假象。

本模块只处理 Windows；其它平台由调用方退化为 `chmod`（Linux 交付需由部署
规范做系统账户隔离）。
"""

from __future__ import annotations

import ctypes
import os
import sys
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path

IS_WINDOWS = sys.platform == "win32"

# SetNamedSecurityInfoW / GetNamedSecurityInfoW
SE_FILE_OBJECT = 1
OWNER_SECURITY_INFORMATION = 0x00000001
DACL_SECURITY_INFORMATION = 0x00000004
PROTECTED_DACL_SECURITY_INFORMATION = 0x80000000
SE_DACL_PROTECTED = 0x1000

# ACL / ACE
ACL_REVISION = 2
ACL_SIZE_INFORMATION_CLASS = 2
ACCESS_ALLOWED_ACE_TYPE = 0
ACCESS_DENIED_ACE_TYPE = 1
INHERITED_ACE = 0x10
OBJECT_INHERIT_ACE = 0x1
CONTAINER_INHERIT_ACE = 0x2
FILE_ALL_ACCESS = 0x1F01FF

# TokenUser
TOKEN_QUERY = 0x0008
TOKEN_USER_INFORMATION_CLASS = 1

# 普通账户/匿名访问会落到的组。设置后如果还允许其中任何一个，视为加固失败。
ADMINISTRATORS_SID = "S-1-5-32-544"

# 管理员账户创建的文件，Owner 常是 BUILTIN\Administrators 而不是用户 SID。
# 这仍属于受信所有者；普通账户（Users）不在其中。
TRUSTED_OWNER_SIDS = frozenset({ADMINISTRATORS_SID})

BROAD_SIDS = frozenset(
    {
        "S-1-1-0",  # Everyone
        "S-1-5-11",  # Authenticated Users
        "S-1-5-32-545",  # BUILTIN\Users
        "S-1-5-32-546",  # BUILTIN\Guests
        "S-1-5-4",  # INTERACTIVE
        "S-1-5-2",  # NETWORK
    }
)


class AclError(RuntimeError):
    """无法设置/读取/验证私有 ACL。"""


@dataclass(frozen=True, slots=True)
class Ace:
    sid: str
    allowed: bool
    inherited: bool
    mask: int


@dataclass(frozen=True, slots=True)
class AclInfo:
    protected: bool
    owner_sid: str
    aces: tuple[Ace, ...]


class _SID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]


class _TOKEN_USER(ctypes.Structure):
    _fields_ = [("User", _SID_AND_ATTRIBUTES)]


class _ACL(ctypes.Structure):
    _fields_ = [
        ("AclRevision", ctypes.c_ubyte),
        ("Sbz1", ctypes.c_ubyte),
        ("AclSize", wintypes.WORD),
        ("AceCount", wintypes.WORD),
        ("Sbz2", wintypes.WORD),
    ]


class _ACL_SIZE_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("AceCount", wintypes.DWORD),
        ("AclBytesInUse", wintypes.DWORD),
        ("AclBytesFree", wintypes.DWORD),
    ]


class _ACE_HEADER(ctypes.Structure):
    _fields_ = [
        ("AceType", ctypes.c_ubyte),
        ("AceFlags", ctypes.c_ubyte),
        ("AceSize", wintypes.WORD),
    ]


def _require_windows() -> None:
    if not IS_WINDOWS:
        raise AclError("Windows ACL 只在 Windows 上可用")


def _dll(name: str) -> ctypes.CDLL:
    win_dll = getattr(ctypes, "WinDLL", None)
    if win_dll is None:  # pragma: no cover - 仅在非 Windows 命中
        raise AclError("当前平台没有 WinDLL")
    return win_dll(name, use_last_error=True)  # type: ignore[no-any-return]


def _last_error(prefix: str) -> AclError:
    code = ctypes.get_last_error()
    return AclError(f"{prefix}（WinError {code}: {ctypes.FormatError(code)}）")


def _sid_to_string(sid: ctypes.c_void_p | int) -> str:
    advapi32 = _dll("advapi32")
    advapi32.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
    advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
    out = wintypes.LPWSTR()
    if not advapi32.ConvertSidToStringSidW(sid, ctypes.byref(out)):
        raise _last_error("ConvertSidToStringSidW 失败")
    try:
        return str(out.value)
    finally:
        kernel32 = _dll("kernel32")
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree.restype = ctypes.c_void_p
        kernel32.LocalFree(out)


def _string_to_sid(value: str) -> ctypes.c_void_p:
    advapi32 = _dll("advapi32")
    advapi32.ConvertStringSidToSidW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p)]
    advapi32.ConvertStringSidToSidW.restype = wintypes.BOOL
    out = ctypes.c_void_p()
    if not advapi32.ConvertStringSidToSidW(value, ctypes.byref(out)):
        raise _last_error(f"ConvertStringSidToSidW 失败：{value}")
    return out


def current_user_sid() -> str:
    """当前进程账户 SID（不依赖本地化账户名）。"""
    _require_windows()
    advapi32 = _dll("advapi32")
    kernel32 = _dll("kernel32")
    advapi32.OpenProcessToken.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.GetTokenInformation.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.GetTokenInformation.restype = wintypes.BOOL
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE

    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(
        kernel32.GetCurrentProcess(), TOKEN_QUERY, ctypes.byref(token)
    ):
        raise _last_error("OpenProcessToken 失败")
    try:
        size = wintypes.DWORD(0)
        advapi32.GetTokenInformation(
            token, TOKEN_USER_INFORMATION_CLASS, None, 0, ctypes.byref(size)
        )
        buffer = ctypes.create_string_buffer(size.value)
        if not advapi32.GetTokenInformation(
            token, TOKEN_USER_INFORMATION_CLASS, buffer, size, ctypes.byref(size)
        ):
            raise _last_error("GetTokenInformation 失败")
        token_user = ctypes.cast(buffer, ctypes.POINTER(_TOKEN_USER)).contents
        return _sid_to_string(token_user.User.Sid)
    finally:
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle(token)


def read_acl(path: Path) -> AclInfo:
    """读取目标对象的 DACL（只读，用于验证与验收取证）。"""
    _require_windows()
    advapi32 = _dll("advapi32")
    kernel32 = _dll("kernel32")
    advapi32.GetNamedSecurityInfoW.argtypes = [
        wintypes.LPCWSTR,
        ctypes.c_int,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
    ]
    advapi32.GetNamedSecurityInfoW.restype = wintypes.DWORD
    advapi32.GetSecurityDescriptorControl.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.WORD),
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.GetSecurityDescriptorControl.restype = wintypes.BOOL
    advapi32.GetAclInformation.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.c_int,
    ]
    advapi32.GetAclInformation.restype = wintypes.BOOL
    advapi32.GetAce.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p)]
    advapi32.GetAce.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p

    descriptor = ctypes.c_void_p()
    dacl = ctypes.c_void_p()
    owner_sid = ctypes.c_void_p()
    rc = advapi32.GetNamedSecurityInfoW(
        str(path),
        SE_FILE_OBJECT,
        OWNER_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION,
        ctypes.byref(owner_sid),
        None,
        ctypes.byref(dacl),
        None,
        ctypes.byref(descriptor),
    )
    if rc != 0:
        raise AclError(f"GetNamedSecurityInfoW({path}) 失败：WinError {rc}")

    try:
        owner = _sid_to_string(owner_sid)
        control = wintypes.WORD()
        revision = wintypes.DWORD()
        if not advapi32.GetSecurityDescriptorControl(
            descriptor, ctypes.byref(control), ctypes.byref(revision)
        ):
            raise _last_error("GetSecurityDescriptorControl 失败")
        protected = bool(control.value & SE_DACL_PROTECTED)

        aces: list[Ace] = []
        if dacl:
            info = _ACL_SIZE_INFORMATION()
            if not advapi32.GetAclInformation(
                dacl,
                ctypes.byref(info),
                ctypes.sizeof(info),
                ACL_SIZE_INFORMATION_CLASS,
            ):
                raise _last_error("GetAclInformation 失败")
            for index in range(info.AceCount):
                ace_ptr = ctypes.c_void_p()
                if not advapi32.GetAce(dacl, index, ctypes.byref(ace_ptr)):
                    raise _last_error("GetAce 失败")
                if not ace_ptr.value:
                    continue
                header = ctypes.cast(ace_ptr, ctypes.POINTER(_ACE_HEADER)).contents
                # 只登记允许/拒绝 ACE；强制完整性标签（0x11）等不参与判定
                if header.AceType not in (ACCESS_ALLOWED_ACE_TYPE, ACCESS_DENIED_ACE_TYPE):
                    continue
                sid = _sid_to_string(ace_ptr.value + 8)
                mask = ctypes.cast(
                    ace_ptr.value + 4, ctypes.POINTER(wintypes.DWORD)
                ).contents.value
                aces.append(
                    Ace(
                        sid=sid,
                        allowed=header.AceType == ACCESS_ALLOWED_ACE_TYPE,
                        inherited=bool(header.AceFlags & INHERITED_ACE),
                        mask=int(mask),
                    )
                )
        return AclInfo(protected=protected, owner_sid=owner, aces=tuple(aces))
    finally:
        kernel32.LocalFree(descriptor)


def _assert_not_reparse(path: Path) -> None:
    is_symlink = path.is_symlink()
    is_junction = bool(getattr(path, "is_junction", lambda: False)())
    if is_symlink or is_junction:
        raise AclError(f"{path} 是 symlink/junction；拒绝给可被替换的路径加固密钥")


def harden(path: Path, *, directory: bool) -> None:
    """把 DACL 重建为「仅当前账户 + 禁止继承」，并立即验证。

    这是覆盖式写入，不是追加：目标上原有的显式/继承 ACE 都会被替换，
    避免 `icacls /grant` 那种"旧的 Users 允许项还在"的假加固。

    加固前校验所有者：若文件属于别的账户，即使当前账户能改 DACL，
    对方仍可改回；这种情况必须人工处理，不能静默"加固"。
    """
    _require_windows()
    if not path.exists():
        raise AclError(f"目标不存在，无法加固：{path}")
    _assert_not_reparse(path)
    me = current_user_sid()
    current = read_acl(path)
    if current.owner_sid not in {me, *TRUSTED_OWNER_SIDS}:
        raise AclError(
            f"{path} 的所有者是 {current.owner_sid}，不是当前账户 {me} 或受信管理员组；"
            "拒绝加固（先用正确账户接管或重新 provision/restore）"
        )
    advapi32 = _dll("advapi32")
    kernel32 = _dll("kernel32")
    advapi32.InitializeAcl.argtypes = [ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD]
    advapi32.InitializeAcl.restype = wintypes.BOOL
    advapi32.AddAccessAllowedAceEx.argtypes = [
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
    ]
    advapi32.AddAccessAllowedAceEx.restype = wintypes.BOOL
    advapi32.GetLengthSid.argtypes = [ctypes.c_void_p]
    advapi32.GetLengthSid.restype = wintypes.DWORD
    advapi32.SetNamedSecurityInfoW.argtypes = [
        wintypes.LPCWSTR,
        ctypes.c_int,
        wintypes.DWORD,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    advapi32.SetNamedSecurityInfoW.restype = wintypes.DWORD
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p

    sid = _string_to_sid(current_user_sid())
    try:
        sid_length = int(advapi32.GetLengthSid(sid))
        ace_size = 8 + sid_length  # ACE_HEADER(4) + Mask(4) + SID
        acl_size = ctypes.sizeof(_ACL) + ace_size
        acl_buffer = ctypes.create_string_buffer(acl_size)
        if not advapi32.InitializeAcl(acl_buffer, acl_size, ACL_REVISION):
            raise _last_error("InitializeAcl 失败")
        ace_flags = OBJECT_INHERIT_ACE | CONTAINER_INHERIT_ACE if directory else 0
        if not advapi32.AddAccessAllowedAceEx(
            acl_buffer, ACL_REVISION, ace_flags, FILE_ALL_ACCESS, sid
        ):
            raise _last_error("AddAccessAllowedAceEx 失败")
        rc = advapi32.SetNamedSecurityInfoW(
            str(path),
            SE_FILE_OBJECT,
            DACL_SECURITY_INFORMATION | PROTECTED_DACL_SECURITY_INFORMATION,
            None,
            None,
            ctypes.cast(acl_buffer, ctypes.c_void_p),
            None,
        )
        if rc != 0:
            raise AclError(f"SetNamedSecurityInfoW({path}) 失败：WinError {rc}")
    finally:
        kernel32.LocalFree(sid)

    assert_private(path, directory=directory)


def assert_private(path: Path, *, directory: bool) -> AclInfo:
    """验证 DACL 只允许当前账户，且不再继承父目录权限。

    普通账户（Everyone / Users / Authenticated Users / INTERACTIVE）只要还有
    一条允许 ACE 就失败——这正是"普通账户能读密钥"的判定口径。
    """
    _require_windows()
    info = read_acl(path)
    if not info.protected:
        raise AclError(f"{path} 的 DACL 仍可继承父目录权限，普通账户可能读到密钥")
    owner = current_user_sid()
    if info.owner_sid not in {owner, *TRUSTED_OWNER_SIDS}:
        raise AclError(
            f"{path} 的所有者是 {info.owner_sid}，不是当前账户 {owner} 或受信管理员组；"
            "对方仍可重写 DACL，不能视为私有"
        )
    allowed = [ace for ace in info.aces if ace.allowed]
    if not any(ace.sid == owner for ace in allowed):
        raise AclError(f"{path} 没有给当前账户（{owner}）读权限")
    strangers = sorted({ace.sid for ace in allowed if ace.sid != owner})
    if strangers:
        raise AclError(f"{path} 仍允许其他主体访问：{', '.join(strangers)}")
    return info


def describe(path: Path) -> dict[str, object]:
    """给验收记录用的 ACL 摘要（只有 SID 与掩码，不含任何密钥内容）。"""
    _require_windows()
    info = read_acl(path)
    return {
        "path": str(path),
        "protected": info.protected,
        "owner_sid": info.owner_sid,
        "aces": [
            {"sid": ace.sid, "allowed": ace.allowed, "inherited": ace.inherited, "mask": ace.mask}
            for ace in info.aces
        ],
    }


def tighten(path: Path, *, directory: bool) -> None:
    """跨平台入口：Windows 走 DACL；其它平台退化为 chmod（由部署规范兜底）。"""
    if IS_WINDOWS:
        harden(path, directory=directory)
        return
    _assert_not_reparse(path)
    os.chmod(path, 0o700 if directory else 0o600)


__all__ = [
    "AclError",
    "AclInfo",
    "Ace",
    "assert_private",
    "current_user_sid",
    "describe",
    "harden",
    "read_acl",
    "tighten",
]
