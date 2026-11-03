"""Token 计数。

L1 §4.3 的 250–450 / 800–1500 是按 token 计的试验值，但 Embedding 模型尚未由
ADR-04/06 确定，无法锁定真实 tokenizer。这里提供一个可替换的近似计数器：

- 中日韩字符按 1 token/字计（BGE 系列的中文分词接近此量级）；
- 拉丁字母与数字串按 1.4 token/词计；
- 其余非空白符号按 0.4 token/个计。

近似值是切片边界的近似，不是质量承诺。模型确定后必须换成真实 tokenizer，
并同步更新 tokenizer_id —— 否则同一份文档在不同模型下会产生不同切片，
引用位置无法复现。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Protocol

_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\u3040-\u30ff\uac00-\ud7af]")
_LATIN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/+\-]*")


class TokenCounter(Protocol):
    # 用只读属性声明：frozen dataclass 的实现是只读的，写成可变属性会不兼容
    @property
    def id(self) -> str: ...

    def count(self, text: str) -> int: ...


@dataclass(frozen=True, slots=True)
class ApproxCjkTokenCounter:
    id: str = "approx-cjk-v1"

    def count(self, text: str) -> int:
        if not text:
            return 0
        cjk = len(_CJK.findall(text))
        latin = len(_LATIN.findall(text))
        other = sum(
            1 for char in text if not char.isspace() and not _CJK.match(char) and not char.isalnum()
        )
        return max(1, math.ceil(cjk + latin * 1.4 + other * 0.4))
