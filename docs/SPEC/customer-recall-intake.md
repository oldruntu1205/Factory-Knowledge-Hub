# 客户语料 recall 评测集：提交格式与验收口径

> 目的：用**客户真实资料 + 真实问题**决定混合检索是否值得在生产启用。
> 仓库文档的 0.68 → 0.84 只证明相对增益，不能替代客户语料验收。

## 1. 需要你提供什么

### A. 语料目录

放到 `_corpus/customer_docs/`（该目录已 gitignore，不进仓库、不外发）。

支持格式（与解析器一致）：

```text
.md / .markdown / .pdf / .docx / .csv / .tsv / .html / .htm
```

建议 10–50 篇真实资料；PDF 需有可提取文本（扫描件需先 OCR，当前解析器不负责 OCR）。

### B. 评测集 JSON

放到 `_corpus/customer_cases.json`，格式：

```json
{
  "note": "客户语料 recall 评测集；needle 必须在原文中逐字出现",
  "k_default": 10,
  "cases": [
    {
      "id": "C001",
      "query": "这款冷凝管的磨口尺寸是多少",
      "needle": "24/29",
      "doc": "产品手册-冷凝管"
    },
    {
      "id": "C002",
      "query": "抽检不合格整批退回还是可以让步接收",
      "needle": "整批退回",
      "doc": "质量协议"
    }
  ]
}
```

字段约束：

| 字段 | 要求 |
|---|---|
| `id` | 唯一，建议 C001… |
| `query` | 客户真实问法；建议一半术语型、一半口语/换说法型 |
| `needle` | **逐字**出现在对应文档解析文本里的正确片段；不要写"大概意思" |
| `doc` | 语料文件名去掉扩展名（stem）；例如 `产品手册-冷凝管.pdf` → `产品手册-冷凝管` |
| 数量 | 50–100 条；同一问题不要重复（会放大该题权重） |

> `needle` 是"命中判据"，不是"标准答案全文"；写一个足以证明找对了的短串即可。
> 先用校验脚本检查，避免跑到一半才发现 needle 根本不在语料里。

## 2. 先校验（不加载模型、不建库）

```powershell
uv run python scripts/validate_recall_cases.py `
  --corpus _corpus/customer_docs `
  --cases _corpus/customer_cases.json
```

它会检查：数量 50–100、id 唯一、query 不重复、doc 能找到同 stem 文件、
needle 逐字出现在解析文本里。全部通过再跑评测。

## 3. 跑评测（本地 BGE-M3，离线）

```powershell
uv sync --all-groups --extra embedding

$env:HF_ENDPOINT = "https://hf-mirror.com"
$env:HF_HOME = "H:/MyWork/Factory-Knowledge-Hub/.hf-cache"

uv run python -m fkh.tools.recall_benchmark `
  --corpus _corpus/customer_docs `
  --cases _corpus/customer_cases.json `
  --k 10 `
  --pgdata .bench-pgdata `
  --embedder bge-m3 --device cpu `
  --output _evidence/customer_recall_report.json
```

输出：lexical vs hybrid 的 recall@10、doc@10、P@10、两路独占命中、耗时。
逐条结果可复核（每条 top-10 文本、命中/未命中）。

## 4. 验收口径（建议，需你确认后冻结）

| 指标 | 建议下限 | 说明 |
|---|---|---|
| recall@10（片段） | ≥ 0.70 | 与仓库语料 0.68 基线同口径 |
| hybrid vs lexical | hybrid ≥ lexical | 至少不能退化；有增益才值得开 |
| doc@10 | ≥ 0.85 | 区分"没找回"与"切片切断" |
| P@10（文档级代理） | 报告但不设门槛 | 代理指标，高估/低估都可能 |

> 阈值在任何评测结果出来**之前**冻结；不根据结果反向调门槛。
> 如果 hybrid 未达下限，保持默认 `lexical`，把失败样本作为下一轮改进输入。

## 5. 我不会做的事

- 不把客户语料发到任何远程 API；embedding 只用本地权重。
- 不用合成问题冒充客户问题；不用仓库文档的结果替代客户验收。
- 不在未达验收线时悄悄把实例默认切到 hybrid。
