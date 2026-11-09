# 客户目录混合检索试点（2026-11-09）

> 目的：用客户真实产品目录 + 现有 30 条目录询价用例，验证 **真实 BGE-M3 + 混合召回** 在客户语料上的链路。
> **这不是客户语料验收**：用例是目录询价（精确通道本应处理），不是描述类客户问题；数量 30，未达到 50–100 的验收集要求。

## 语料与用例

| 项 | 值 |
|---|---|
| 语料 | `_corpus/catalog_2023.md`（客户产品目录，1 篇，约 117KB） |
| 用例 | `_corpus/catalog_cases.json`（30 条目录询价，自动生成；试点时补 `doc=catalog_2023`） |
| 检索 | lexical vs hybrid（BGE-M3，CPU，RRF k=60，top-10） |
| 评测库 | `.bench-pgdata`（临时，已 gitignore） |

## 结果

| 指标 | lexical | hybrid | 差值 |
|---|---|---|---|
| recall@10（片段） | **0.500** | **0.567** | **+0.067** |
| doc@10 | 1.000 | 1.000 | 0 |
| 仅本路命中 | 0 | 2 | — |
| 两路都未命中 | — | **13** | — |
| 平均耗时 | 30.3ms | 206.1ms | +175.8ms |

- 30 条中 13 条两路都未命中；按 `needle`（含货号+规格+价格整行）判命中，切片/表格行被拆开时不命中。
- doc@10 = 1.0 只说明命中的文档对了；片段级仍只有一半左右。
- 逐条结果（含原文价格）落在 `_corpus/catalog_recall_report_20261109.json`（敏感，不入库、不入仓库）。

## 结论

1. **链路可用**：客户目录能入库，BGE-M3 能编码，hybrid 能返回结果，RRF 有 +2 条独占命中。
2. **不能据此启用生产默认 hybrid**：这是目录询价，产品主路径是确定性 `catalog_items` 精确通道；
   用检索去查货号本来就不是正确路由。片段 recall 0.567 低于客户验收建议线 0.70。
3. **冻结阈值不适用**：50–100 条描述类客户问题 + 人工标注尚未提供；本轮不改默认（仍 `lexical`），
   也不把该试点写入“客户语料验收通过”。
4. **下一步**：按 `docs/SPEC/customer-recall-intake.md` 提供真实客户问题集与语料；或先修
   “目录询价不应走检索”的路由问题（属产品行为裁决，不在本轮自动改）。

## 复现

```powershell
$env:HF_ENDPOINT = "https://hf-mirror.com"
$env:HF_HOME = "H:/MyWork/Factory-Knowledge-Hub/.hf-cache"
uv run python scripts/validate_recall_cases.py `
  --corpus _corpus/catalog_2023.md `
  --cases _corpus/catalog_recall_cases_20261109.json --min 1 --max 40
uv run python -m fkh.tools.recall_benchmark `
  --corpus _corpus/catalog_2023.md `
  --cases _corpus/catalog_recall_cases_20261109.json `
  --k 10 --pgdata .bench-pgdata --embedder bge-m3 --device cpu `
  --output _corpus/catalog_recall_report_20261109.json
```
