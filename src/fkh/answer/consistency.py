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

RULE_VERSION = "literal-support-v1"

# 货号：1–3 个大写字母 + 5–8 位数字，两侧要边界
_CODE = re.compile(r"(?<![A-Za-z0-9])([A-Z]{1,3}\d{5,8})(?![0-9])")

# 单位白名单：固定、区分大小写、带后向边界（不能是更长单词的前缀）
_UNITS = ("kg", "mm", "cm", "ml", "g", "l", "L", "%", "℃", "元", "只/箱")
_UNIT_PATTERN = "(?:" + "|".join(sorted(_UNITS, key=len, reverse=True)) + ")(?![A-Za-z])"

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


def _appears_with_boundary(needle: str, haystack: str) -> bool:
    """字面出现且左侧不是数字/小数点（`14mm` 不能被 `114mm` 支持）。"""
    if not needle:
        return False
    pattern = r"(?<![\d.])" + re.escape(needle)
    return re.search(pattern, haystack) is not None


def _supported_number(claim: str, normalized_evidence: str) -> bool:
    """带单位数值的支持判断。

    严格先比"数值+单位+结构"。目录表格常把单位写在表头（单元格里只有 `24×24`），
    因此对**区间/尺寸**再放宽一次：结构体（如 `24×24`、`14-70`）出现即算支持。
    单值（如 `39.00元`）不放宽——价格必须带单位一起对得上。
    """
    normalized = normalize(claim)
    if _appears_with_boundary(normalized, normalized_evidence):
        return True
    if "×" in normalized or "-" in normalized:
        core = re.sub(r"[a-zA-Z%℃元只/箱]+$", "", normalized)
        return _appears_with_boundary(core, normalized_evidence)
    return False


def check_answer(answer: str, evidence_texts: list[str]) -> ClaimCheck:
    """核验答案里的货号与带单位数值是否字面出现在**被引用的证据**中。"""
    codes, numbers = extract_claims(answer)
    joined = "\n".join(evidence_texts)
    normalized_evidence = normalize(joined)

    missing_codes = tuple(code for code in codes if not _supported_code(code, joined))
    missing_numbers = tuple(
        claim for claim in numbers if not _supported_number(claim, normalized_evidence)
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
