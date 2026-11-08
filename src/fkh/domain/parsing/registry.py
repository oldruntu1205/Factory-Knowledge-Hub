"""按扩展名 / MIME 分派解析器。

不支持的类型必须显式失败。静默降级为"按纯文本读"会把 PDF 二进制当正文写入索引，
产生的错误比直接拒绝严重得多。
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from fkh.domain.parsing.common import sha256_hex
from fkh.domain.parsing.csv_parser import parse_csv
from fkh.domain.parsing.docx import parse_docx
from fkh.domain.parsing.html import parse_html
from fkh.domain.parsing.markdown import parse_markdown
from fkh.domain.parsing.pdf import parse_pdf
from fkh.domain.parsing.types import ParsedDocument, ParseError

Parser = Callable[..., ParsedDocument]

_SUFFIX_PARSERS: dict[str, Parser] = {
    ".md": parse_markdown,
    ".markdown": parse_markdown,
    ".pdf": parse_pdf,
    ".docx": parse_docx,
    ".csv": parse_csv,
    ".tsv": parse_csv,
    ".html": parse_html,
    ".htm": parse_html,
}

# 供语料目录收集/验收工具使用；新增解析器时必须同步这个集合。
SUPPORTED_SUFFIXES = frozenset(_SUFFIX_PARSERS)

_MIME_PARSERS: dict[str, Parser] = {
    "text/markdown": parse_markdown,
    "text/x-markdown": parse_markdown,
    "application/pdf": parse_pdf,
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": parse_docx,
    "text/csv": parse_csv,
    "text/tab-separated-values": parse_csv,
    "text/html": parse_html,
    "application/xhtml+xml": parse_html,
}

# .doc（旧二进制格式）不在支持范围内：需要 LibreOffice 转换，属独立处理链路
_KNOWN_UNSUPPORTED = {".doc": "旧版 DOC 需先转换为 DOCX", ".xlsx": "表格请导出为 CSV"}


class UnsupportedFormatError(ParseError):
    pass


def resolve_parser(*, filename: str, mime_type: str | None = None) -> Parser:
    if mime_type and mime_type in _MIME_PARSERS:
        return _MIME_PARSERS[mime_type]

    suffix = Path(filename).suffix.lower()
    if suffix in _SUFFIX_PARSERS:
        return _SUFFIX_PARSERS[suffix]

    if suffix in _KNOWN_UNSUPPORTED:
        raise UnsupportedFormatError(f"{suffix} 暂不支持：{_KNOWN_UNSUPPORTED[suffix]}")

    raise UnsupportedFormatError(f"无法识别的文件类型：suffix={suffix or '(无)'} mime={mime_type or '(无)'}")


def parse_document(
    content: bytes,
    *,
    source_id: str,
    filename: str,
    mime_type: str | None = None,
    language: str = "zh-CN",
    source_sha256: str | None = None,
) -> ParsedDocument:
    parser = resolve_parser(filename=filename, mime_type=mime_type)
    digest = source_sha256 or sha256_hex(content)
    return parser(content, source_id=source_id, source_sha256=digest, language=language)
