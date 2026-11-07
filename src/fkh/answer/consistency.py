"""答案—证据一致性检查（确定性，不引入模型）。

2026-11-06 L1 裁决（`_evidence/l1_reply_consistency_20261106.md`）：
批准为**「货号及带单位数值的字面支持检查」**，要求

1. 任一货号或数字 claim 未获支持 → 降为 `HUMAN_REVIEW_REQUIRED`，原因码 `UNSUPPORTED_CLAIM`；
2. 只跟**有效引用的证据块**比对，不回退到全部证据；
3. 区间 `14-70mm`、尺寸 `24×24mm` 作为**整体**核验，不允许两处互不相关的数字拼出支持；
4. 固定单位白名单、带数字与单位边界（`14` 不能被 `114` 支持，短货号不能被长货号支持）；
5. 检查通过只表示"未发现所覆盖的字面异常"，**不证明语义一致**。

它抓不住"价格不含税"这类没有数字/货号的断言——这是裁决明确写下的边界，不要据此宣称解决幻觉。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

RULE_VERSION = "literal-support-v4"

# 货号：1–3 个大写字母 + 5–8 位数字，两侧要边界
_CODE = re.compile(r"(?<![A-Za-z0-9])([A-Z]{1,3}\d{5,8})(?![0-9])")

# 单位白名单：固定、区分大小写、带后向边界（不能是更长单词的前缀）
#
# v4（2026-11-07 L1 裁决）：补上制造业高频量纲。此前白名单只有 11 个单位，
# 导致 `0.2MPa` / `60秒` / `1.5倍` 这类 claim 根本不被提取——实测出现
# 「证据写 0.2MPa、答案编 1.5倍工作压力，仍判 ANSWERED 并附真实引用」。
#
# 排序按长度倒序，保证 `MPa` 不会被拆成 `Pa`、`N·m` 不被拆成 `m`。
# 刻意不含 `号` / `目`（型号、序号、规格标签歧义大，需上下文识别另行立项）；
# `倍` 只做字面支持，**不与百分比或裸数互认**（裁决 §3）。
_UNITS = (
    # 压力 / 力 / 扭矩
    "MPa", "kPa", "Pa", "bar", "kgf", "N·m", "Nm",
    # 时间
    "秒", "分钟", "小时", "s", "min", "h",
    # 电 / 功率
    "V", "A", "kW", "W",
    # 转速 / 温度 / 长度 / 容积 / 质量
    "rpm", "°C", "℃", "mm", "cm", "ml", "kg", "g", "l", "L",
    # 计数与计量
    "%", "元", "只/箱",
    # 倍数：只做字面匹配
    "倍",
)
# 单字母单位（s/V/A/W/g/l/L/h）必须两侧都有边界，否则会从 `MPa`、`Nm` 等复合单位里切出一段
_SINGLE_LETTER_UNITS = frozenset({"s", "V", "A", "W", "g", "l", "L", "h"})
_UNIT_PATTERN = "(?:" + "|".join(sorted(_UNITS, key=len, reverse=True)) + ")(?![A-Za-z0-9])"

_NUM = r"\d+(?:\.\d+)?"
_RANGE = re.compile(rf"(?<![\d.])({_NUM})\s*[-~—至]\s*({_NUM})\s*({_UNIT_PATTERN})")
_DIMENSION = re.compile(rf"(?<![\d.])({_NUM})\s*[×xX*]\s*({_NUM})\s*({_UNIT_PATTERN})")
_SINGLE = re.compile(rf"(?<![\d.])({_NUM})\s*({_UNIT_PATTERN})")

_FULLWIDTH = str.maketrans(
    "０１２３４５６７８９．，：（）－～＊", "0123456789.,:()-~*"
)


def _to_halfwidth(text: str) -> str:
    """全角 ASCII（数字/字母/标点）转半角。"""
    chars = []
    for char in text:
        code = ord(char)
        if 0xFF01 <= code <= 0xFF5E:
            chars.append(chr(code - 0xFEE0))
        elif code == 0x3000:
            chars.append(" ")
        else:
            chars.append(char)
    return "".join(chars)


def normalize(text: str) -> str:
    """全角转半角、去空白、统一乘号与连字符，便于字面对比。"""
    normalized = _to_halfwidth(text)
    normalized = normalized.replace("*", "×")
    normalized = re.sub(r"[xX](?=\d)", "×", normalized)  # 24x24 → 24×24
    normalized = normalized.replace("～", "-").replace("—", "-")
    return re.sub(r"\s+", "", normalized)


@dataclass(frozen=True, slots=True)
class ClaimCheck:
    codes_total: int
    codes_supported: int
    numbers_total: int
    numbers_supported: int
    unsupported_codes: tuple[str, ...] = ()
    unsupported_numbers: tuple[str, ...] = ()

    @property
    def supported(self) -> bool:
        return not self.unsupported_codes and not self.unsupported_numbers

    @property
    def applicable(self) -> bool:
        return self.codes_total > 0 or self.numbers_total > 0

    def to_public(self) -> dict[str, Any]:
        """给调用方看的视图（2026-11-07）。

        只回**答案里**被判定不受支持的货号/数值 token——它们本来就出现在 answer 正文里，
        不额外泄漏证据内容。附带 fixed disclaimer：这项检查只做字面支持，不证明语义一致。
        """
        return {
            "rule": RULE_VERSION,
            "applicable": self.applicable,
            "supported": self.supported,
            "codes": {
                "total": self.codes_total,
                "supported": self.codes_supported,
                "unsupported": list(self.unsupported_codes),
            },
            "numbers": {
                "total": self.numbers_total,
                "supported": self.numbers_supported,
                "unsupported": list(self.unsupported_numbers),
            },
            "disclaimer": (
                "仅表示所覆盖的货号/带单位数值在**被引用的证据**中字面出现；"
                "通过不代表语义一致，未通过也不代表一定错误（可能是改写或单位换算）。"
            ),
        }

    @property
    def reason_codes(self) -> tuple[str, ...]:
        """命中的类别码（2026-11-07 L1：内部审计/日志必须显式记录类别）。"""
        codes: list[str] = []
        if self.unsupported_codes:
            codes.append("UNSUPPORTED_CODE")
        if self.unsupported_numbers:
            codes.append("UNSUPPORTED_NUMBER")
        return tuple(codes)


def extract_claims(answer: str) -> tuple[list[str], list[str]]:
    """抽出货号与「带单位的数值」两类 claim；区间/尺寸保持结构，不拆散。"""
    codes = list(dict.fromkeys(_CODE.findall(answer)))

    numbers: list[str] = []
    consumed: list[tuple[int, int]] = []
    for pattern, join in ((_RANGE, "-"), (_DIMENSION, "×")):
        for match in pattern.finditer(answer):
            left, right, unit = match.groups()
            numbers.append(f"{left}{join}{right}{unit}")
            consumed.append(match.span())

    for match in _SINGLE.finditer(answer):
        if any(start <= match.start() < end for start, end in consumed):
            continue
        value, unit = match.groups()
        numbers.append(f"{value}{unit}")

    return codes, list(dict.fromkeys(numbers))


def _supported_code(claim: str, evidence: str) -> bool:
    return any(match.group(1) == claim for match in _CODE.finditer(evidence))


_UNIT_TOKEN = re.compile("|".join(sorted(_UNITS, key=len, reverse=True)))
_HEADER_HINTS = ("货号", "编号", "型号")


def _cell_unit(tokens: list[str], index: int) -> str | None:
    """取某一列声明/自带的单位（来自该列表头，或该单元格本身）。"""
    for token in (tokens[index] if index < len(tokens) else "",):
        match = _UNIT_TOKEN.search(normalize(token))
        if match is not None:
            return match.group(0)
    return None


def _header_for(lines: list[str], row_index: int) -> list[str] | None:
    """向上找最近的一行表头（含 货号/编号/型号 且列数不少于数据行）。"""
    row_columns = len(lines[row_index].split())
    for index in range(row_index - 1, max(-1, row_index - 4), -1):
        candidate = lines[index]
        tokens = candidate.split()
        if len(tokens) < row_columns:
            continue
        if any(hint in candidate for hint in _HEADER_HINTS):
            return tokens
    return None


def _unit_belongs_to_cell(core: str, evidence: str, unit: str) -> bool:
    """结构体所在**那一列**必须自带或由该列表头声明同一个单位。

    2026-11-07 L1 复审：单位的"存在"不等于"属于该结构"——
    同一行别的列写着 `200mm`，不能据此认定 `24*24` 的单位是 mm。
    因此按**列位置**对齐表头取单位；对不上或表头没写单位，一律不支持。
    """
    lines = evidence.splitlines()
    for index, line in enumerate(lines):
        tokens = line.split()
        normalized = [normalize(token) for token in tokens]
        if core not in normalized:
            continue
        column = normalized.index(core)
        header = _header_for(lines, index)
        if header is None:
            continue
        header_unit = _cell_unit([normalize(token) for token in header], column)
        if header_unit == unit:
            return True
    return False


def _appears_with_boundary(needle: str, haystack: str) -> bool:
    """字面出现且左侧不是数字/小数点（`14mm` 不能被 `114mm` 支持）。"""
    if not needle:
        return False
    pattern = r"(?<![\d.])" + re.escape(needle)
    return re.search(pattern, haystack) is not None


def _supported_number(claim: str, evidence_texts: list[str]) -> bool:
    """带单位数值的支持判断。

    先比"数值+单位+结构"的完整字面；对**区间/尺寸**允许单元格只写结构体（目录把单位放表头），
    但必须能在同一行或邻近表头里找到**同一个单位**——单位缺失或单位不同都不算支持。
    单值（如 `39.00元`）不放宽：价格必须带单位一起对得上。
    """
    normalized = normalize(claim)
    if any(_appears_with_boundary(normalized, normalize(text)) for text in evidence_texts):
        return True

    if "×" not in normalized and "-" not in normalized:
        return False

    unit_match = re.search(r"[a-zA-Z%℃元只/箱]+$", normalized)
    if unit_match is None:
        return False
    unit = unit_match.group(0)
    core = normalized[: unit_match.start()]
    return any(_unit_belongs_to_cell(core, text, unit) for text in evidence_texts)


def check_answer(answer: str, evidence_texts: list[str]) -> ClaimCheck:
    """核验答案里的货号与带单位数值是否字面出现在**被引用的证据**中。"""
    codes, numbers = extract_claims(answer)
    joined = "\n".join(evidence_texts)

    missing_codes = tuple(code for code in codes if not _supported_code(code, joined))
    missing_numbers = tuple(
        claim for claim in numbers if not _supported_number(claim, evidence_texts)
    )

    return ClaimCheck(
        codes_total=len(codes),
        codes_supported=len(codes) - len(missing_codes),
        numbers_total=len(numbers),
        numbers_supported=len(numbers) - len(missing_numbers),
        unsupported_codes=missing_codes,
        unsupported_numbers=missing_numbers,
    )


__all__ = ["RULE_VERSION", "ClaimCheck", "check_answer", "extract_claims", "normalize"]
