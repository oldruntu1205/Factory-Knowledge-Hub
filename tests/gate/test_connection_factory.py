"""门禁：连接池只能经 `tenant_connection` 借出（L1 2026-11-07 放行条件 4）。

业务代码直接 `pool.connection()` 会绕过租户绑定与清理，导致
"某条路径忘了绑定"或"连接带残留租户回到池里"。这条测试把约束变成可失败的检查。
"""

from __future__ import annotations

import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "fkh"

ALLOWED = {SRC / "api" / "db.py"}  # 唯一允许直接使用连接池的模块

PATTERN = re.compile(r"\bpool\.connection\(|\.connection\(\)\s+as\s+conn")


def test_pool_connections_only_through_tenant_factory() -> None:
    offenders: list[str] = []
    for path in SRC.rglob("*.py"):
        if path in ALLOWED:
            continue
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            if "pool.connection(" in line:
                offenders.append(f"{path.relative_to(SRC)}:{lineno}: {line.strip()}")

    assert not offenders, (
        "禁止绕过 tenant_connection 直接从连接池取连接（会把租户上下文丢给调用者记得）：\n"
        + "\n".join(offenders)
    )
