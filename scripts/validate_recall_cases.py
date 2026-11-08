"""客户语料 recall 评测集校验（在跑昂贵 embedding 之前先把数据问题挡住）。

校验：
- 用例数 50–100；
- 每条的 id/query/needle/doc 字段完整、id 唯一；
- doc 必须对应语料文件名 stem；
- needle 必须**逐字**出现在对应语料的解析文本里（否则 recall 永远不可能命中）；
- 语料只包含解析器支持的格式。

用法：
    uv run python scripts/validate_recall_cases.py \
        --corpus _corpus/customer_docs \
        --cases _corpus/customer_cases.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from fkh.domain.parsing.registry import parse_document  # noqa: E402
from fkh.tools.recall_benchmark import _collect_files, load_cases  # noqa: E402


def _document_text(path: Path) -> str:
    parsed = parse_document(
        path.read_bytes(),
        source_id=path.stem,
        filename=path.name,
    )
    parts: list[str] = []
    for block in parsed.blocks:
        parts.append(block.text)
        if block.table is not None:
            parts.append(block.table.to_text())
    return "\n".join(parts)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="客户语料 recall 评测集校验")
    parser.add_argument("--corpus", nargs="+", required=True, help="语料目录或文件")
    parser.add_argument("--cases", required=True, help="评测集 JSON")
    parser.add_argument("--min", type=int, default=50)
    parser.add_argument("--max", type=int, default=100)
    args = parser.parse_args(argv)

    files = _collect_files(args.corpus)
    cases = load_cases(args.cases)
    errors: list[str] = []

    if not (args.min <= len(cases) <= args.max):
        errors.append(f"用例数 {len(cases)} 不在 [{args.min}, {args.max}] 范围内")
    ids = [case.id for case in cases]
    if len(set(ids)) != len(ids):
        errors.append("存在重复 id")
    queries = [case.query.strip() for case in cases]
    if len(set(queries)) != len(queries):
        errors.append("存在重复 query（同一问题重复会放大该题的权重）")

    texts: dict[str, str] = {}
    for path in files:
        try:
            texts[path.stem] = _document_text(path)
        except Exception as exc:  # noqa: BLE001 - 校验脚本要报告而不是抛栈
            errors.append(f"解析失败 {path.name}: {type(exc).__name__}: {exc}")
    if not texts:
        errors.append("语料解析后为空")

    for case in cases:
        if case.doc:
            if case.doc not in texts:
                errors.append(f"{case.id}: doc={case.doc!r} 找不到同 stem 的语料文件")
                continue
            candidates = [texts[case.doc]]
        else:
            candidates = list(texts.values())
        if not any(case.needle in text for text in candidates):
            scope = f"doc={case.doc}" if case.doc else "全部语料"
            errors.append(f"{case.id}: needle={case.needle!r} 未逐字出现在 {scope}")

    summary = {
        "corpus_files": len(files),
        "corpus_stems": sorted(texts),
        "cases": len(cases),
        "errors": len(errors),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if errors:
        print("\n校验失败：", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print("\n校验通过：可以跑 recall_benchmark。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
