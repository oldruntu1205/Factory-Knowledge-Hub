"""解析器共用工具。"""

from __future__ import annotations

import hashlib
from importlib.metadata import PackageNotFoundError, version


def package_version(distribution: str) -> str:
    """读取已安装包版本，用于把 parser_id 固定到具体实现版本。

    解析器版本必须进入切片元数据：同一份文档在不同解析器版本下会产生不同切片，
    没有版本号就无法复现某条引用的来源位置。
    """
    try:
        return version(distribution)
    except PackageNotFoundError:
        return "unknown"


def parser_id(name: str, distribution: str) -> str:
    return f"{name}@{package_version(distribution)}"


def sha256_hex(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()
