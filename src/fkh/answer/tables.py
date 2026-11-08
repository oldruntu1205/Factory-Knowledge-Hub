"""证据文本的表格化改写：把「货号 xxx 列1 列2 …」的表行变成带列名的自然语言。

为什么在提示词侧做
------------------
客户目录里的规格/价格是**表格行**（每行一串裸字段）。本地 7B–14B 读这种行经常"看不懂"：
实测把含「试管」的证据整块塞进提示词，模型仍回答"并未提及试管的规格"。
把行改写成 `货号 C190010：直径*长度=10ml，壁厚=1.2`，列名和值是显式配对的，模型才读得动。

约束：
- 只改**送给模型的文本**，不改切片、不改原文、不改检索；
- 认不出表格就原样返回（不做猜测）；
- 表头取自原文里的「货号 / 编号 / 型号」行，列名按位置对齐；对不齐时退化成「列N=值」。
"""

from __future__ import annotations

import re

# 货号形态：1–3 个大写字母 + 5–8 位数字（与目录抽取一致）
_CODE = re.compile(r"^[A-Z]{1,3}\d{5,8}$")


def _is_code(token: str, pattern: str | None) -> bool:
    """货号判定：有实例规则时用规则（fullmatch），否则用工厂正则兜底。"""
    matcher = re.compile(pattern) if pattern else _CODE
    return matcher.fullmatch(token) is not None
# 表头里出现这些词，说明这一行是列名而不是正文
_HEADER_HINTS = ("货号", "编号", "型号")


def render_evidence(text: str, *, code_pattern: str | None = None) -> str:
    """把表行改写成「货号 X：列名=值，列名=值」。非表行原样保留。"""
    lines_out: list[str] = []
    headers: list[str] | None = None

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            lines_out.append("")
            continue

        tokens = line.split()

        # 表头行：含「货号/编号/型号」且至少两列
        if len(tokens) >= 2 and any(hint in line for hint in _HEADER_HINTS):
            headers = tokens
            lines_out.append("表头：" + " | ".join(headers))
            continue

        # 数据行：首列是货号，后面按位置配列名
        if headers is not None and len(tokens) >= 2 and _is_code(tokens[0], code_pattern):
            pairs: list[str] = []
            for index, cell in enumerate(tokens[1:]):
                name = headers[index + 1] if index + 1 < len(headers) else f"列{index + 2}"
                pairs.append(f"{name}={cell}")
            lines_out.append(f"{tokens[0]}：" + "，".join(pairs))
            continue

        lines_out.append(line)

    return "\n".join(lines_out)


def has_table(text: str, *, code_pattern: str | None = None) -> bool:
    """是否检测到至少一行「表头 + 货号行」。仅用于测试与诊断。"""
    rendered = render_evidence(text, code_pattern=code_pattern)
    return any(line.startswith("表头：") for line in rendered.splitlines()) and any(
        line.split("：", 1)[0].strip() and _is_code(line.split("：", 1)[0].strip(), code_pattern)
        for line in rendered.splitlines()
        if "：" in line
    )


__all__ = ["has_table", "render_evidence"]
