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

RULE_VERSION = "literal-support-v2"

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


def _unit_reported_near(core: str, evidence: str, unit: str) -> bool:
    """结构体出现在证据里时，同一行或上方表头必须**明确给出同一个单位**。

    2026-11-07 L1 终审：`24×24mm` 不能被无关的 `24×24` 支持，必须有可确定关联的表头单位且一致。
    做法：定位含结构体的行，把该行与其上一行（表头/邻近行）拼成上下文；claim 的单位必须出现在上下文里。
    单位缺失（表头也没写）视为不支持——"仅结构相同不足以支持"。
    """
    lines = evidence.splitlines()
    for index, line in enumerate(lines):
        if core not in normalize(line):
            continue
        context = line if index == 0 else lines[index - 1] + "\n" + line
        units = {token for token in _UNIT_TOKEN.findall(normalize(context))}
        if unit in units:
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
    return any(_unit_reported_near(core, text, unit) for text in evidence_texts)


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
