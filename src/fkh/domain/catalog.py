"""目录行抽取：把「货号 规格 单价」行变成可校验的结构化事实。

为什么单独做这一层
------------------
客户产品目录里「货号 → 规格/单价」是**确定性对应**，实测文本 RAG 完全不可用
（recall@10 = 0/30 词法、2/30 混合；且 14B 不标引用）。这类问题应走精确查询，
前提是先把行抽成结构化事实。

边界（刻意的保守）
------------------
- 只认「行首货号 + 行尾单一价格」的行；区间价（12.00-15.00）、缺价格、表头行一律**不猜**，
  记为 unsupported，由调用方计数并保留原文；
- 抽取不改变原文与切片，只新增结构；
- 价格同时保留**原文串**与数值：对账以原文为准，数值只用于排序/比较。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

# 工厂默认货号：1–3 个大写字母 + 5–8 位数字（客户目录实测形如 A112420 / C190010）。
# 2026-11-08（0036）：真正的判定规则来自实例配置（catalog_rules）；
# 这里的常量只用于**读路径兜底**与无配置时的显式报错提示。
FACTORY_CODE_PATTERN = r"^[A-Z]{1,3}[0-9]{5,8}$"


def _row_regex(code_pattern: str) -> re.Pattern[str]:
    """把「货号正则」拼成整行正则；兼容带/不带 ^$ 锚点的写法。"""
    body = code_pattern.strip()
    if body.startswith("^"):
        body = body[1:]
    if body.endswith("$") and not body.endswith("\\$"):
        body = body[:-1]
    return re.compile(
        rf"^(?P<code>(?:{body}))\s+"
        r"(?P<spec>\S.*?)"
        r"\s+(?P<price>\d{1,6}(?:\.\d{1,2})?)$"
    )


ROW = _row_regex(FACTORY_CODE_PATTERN)
_RANGE = re.compile(r"^\d{1,6}(?:\.\d{1,2})?\s*[-~～]\s*\d{1,6}(?:\.\d{1,2})?$")


@dataclass(frozen=True, slots=True)
class CatalogRow:
    code: str
    spec: str
    raw_text: str
    price_text: str
    price_value: Decimal | None


@dataclass(frozen=True, slots=True)
class CatalogParse:
    rows: tuple[CatalogRow, ...]
    # 「看起来像货号行」但价格不是单一数值的原文（区间价、缺价、表头等）
    unsupported: tuple[str, ...]

    @property
    def unsupported_count(self) -> int:
        return len(self.unsupported)


def _looks_like_code(token: str, row_regex: re.Pattern[str] | None = None) -> bool:
    return (row_regex or ROW).match(f"{token} x 0.00") is not None


def parse_catalog_text(
    text: str, *, code_pattern: str | None = None
) -> CatalogParse:
    """逐行抽取；同一段文本内重复货号只保留第一条（重复会在入库层被唯一约束暴露）。

    `code_pattern` 由调用方从实例规则加载后传入（显式，不在本函数里查库）；
    未传时使用工厂默认，仅用于读路径/无配置兜底。
    """
    row_regex = _row_regex(code_pattern) if code_pattern else ROW
    rows: list[CatalogRow] = []
    unsupported: list[str] = []
    seen: set[str] = set()

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        match = row_regex.match(line)
        if match is not None:
            code = match.group("code")
            if code in seen:
                unsupported.append(line)
                continue
            seen.add(code)
            price_text = match.group("price")
            try:
                value: Decimal | None = Decimal(price_text.replace(",", ""))
            except InvalidOperation:
                value = None
            if value is None:
                unsupported.append(line)
                continue
            rows.append(
                CatalogRow(
                    code=code,
                    spec=match.group("spec").strip(),
                    raw_text=line,
                    price_text=price_text,
                    price_value=value,
                )
            )
            continue

        # 行首是货号、但整行不构成"单一价格行"：记 unsupported，不猜
        head = line.split(maxsplit=1)[0]
        if _looks_like_code(head, row_regex):
            unsupported.append(line)

    return CatalogParse(rows=tuple(rows), unsupported=tuple(unsupported))


def is_range_price(token: str) -> bool:
    return _RANGE.match(token.strip()) is not None


# ---------------------------------------------------------------------------
# 意图识别（确定性，不经过模型）
# ---------------------------------------------------------------------------
# 货号：1–3 字母 + 5–8 位数字；前后不得紧邻字母数字，避免从长串里截半截
_CODE_IN_QUERY = re.compile(r"(?<![A-Za-z0-9])([A-Za-z]{1,3}\d{5,8})(?![0-9])")
PRICE_WORDS = ("单价", "价格", "报价", "多少钱", "售价", "价位")
SPEC_WORDS = ("规格", "尺寸", "参数", "型号", "标口", "管径", "口径")


"""同版本内货号冲突的占位标记：该货号不再提供精确答案。"""
CONFLICT_MARK = "\x00conflict"


@dataclass(frozen=True, slots=True)
class CatalogExtractionStats:
    """一次抽取的计数：解析成功 / 入表 / 重复 / 冲突 / 不支持格式。"""

    parsed: int = 0
    inserted: int = 0
    duplicates: int = 0
    conflicts: int = 0
    unsupported: int = 0

    def as_warning(self) -> str:
        return (
            f"catalog parsed={self.parsed} inserted={self.inserted} "
            f"duplicates={self.duplicates} conflicts={self.conflicts} "
            f"unsupported={self.unsupported}"
        )


@dataclass(frozen=True, slots=True)
class CatalogIntent:
    code: str
    kind: str  # "price" | "spec"


def detect_catalog_intent(query: str) -> CatalogIntent | None:
    """识别"单货号 + 明确询价/询规格"的查询。

    刻意保守（L1 裁决）：只有出现**恰好一个**货号，且问题确实在问价格或规格时才接管；
    「这个货号是否停产」这类问题仍走 RAG——精确通道不该因为看见货号就抢答。
    同一货号重复出现仍算一个。
    """
    codes: list[str] = []
    for match in _CODE_IN_QUERY.finditer(query):
        code = match.group(1).upper()
        if code not in codes:
            codes.append(code)
    if len(codes) != 1:
        return None

    if any(word in query for word in PRICE_WORDS):
        return CatalogIntent(code=codes[0], kind="price")
    if any(word in query for word in SPEC_WORDS):
        return CatalogIntent(code=codes[0], kind="spec")
    return None


def count_distinct_codes(query: str) -> int:
    """查询里出现的不同货号数（用于审计"为什么没路由"）。"""
    seen: set[str] = set()
    for match in _CODE_IN_QUERY.finditer(query):
        seen.add(match.group(1).upper())
    return len(seen)


__all__ = [
    "CONFLICT_MARK",
    "CatalogExtractionStats",
    "CatalogIntent",
    "CatalogParse",
    "CatalogRow",
    "PRICE_WORDS",
    "SPEC_WORDS",
    "count_distinct_codes",
    "detect_catalog_intent",
    "is_range_price",
    "parse_catalog_text",
]
