# 真实语料 recall@10 评测（2026-11-04）

> 对应 L1 判决 Q2 的「生产启用混合检索仍未授权：真实 BGE-M3 推理与代表性语料 recall@10 尚未实测」。
> 本文件是那次实测的记录与方法说明。**它证明了链路可用与相对增益，不等于客户语料验收通过。**

## 1. 结论先说

| 召回方式 | recall@10（片段级） | doc@10（文档级） | 平均耗时 |
|---|---|---|---|
| 词法（现行：字级 **AND**） | **0.060**（3/50） | 0.060 | 1.8 ms |
| 词法（**OR** 变体，实验） | **0.660**（33/50） | — | 未测（同量级） |
| 混合（BGE-M3 + 词法，RRF k=60） | **0.800**（40/50） | **0.900**（45/50） | 61 ms |

三条读数：

1. **向量召回确实带来了增益**：相对现行词法从 3/50 提到 40/50，文档级 45/50。
2. **但增益的绝大部分来自词法自身的缺陷，而不是"语义"**：把同一套字级索引的查询构造由
   AND 换成 OR，recall 就从 0.06 跳到 0.66。也就是说，**现行词法对自然语言问题几乎不可用**——
   这是必须先修的 bug 级问题，与是否启用向量无关。
3. 混合相对 OR-词法的净增益是 **+14pp（0.66 → 0.80）**，代价是 **~34 倍延迟**（1.8ms → 61ms）
   与一套模型依赖。是否值得，取决于对"问句口语化程度"的要求，需要客户语料复核。

## 2. 评测口径

- **K = 10**，与契约 `max_results` 默认值一致。
- **片段级命中**：top-10 任一命中块正文包含 `needle`（正确事实的关键串）。
  用「包含答案串」而不是「等于固定 chunk_id」：切片参数一改，绑 chunk_id 的题集就整体失效，
  而且会奖励"切得碎"而不是"找得回"。
- **文档级命中**：top-10 任一命中块属于 `needle` 所在的那篇文档。用来区分
  「没找回」与「找回了但答案被切片边界切成两半」——未命中的 10 条里至少 3 条属于后者
  （H03/H04 的 `needle` 与命中块相邻但在不同 child 里）。
- 两路召回使用**同一批**入库数据、同一 ACL 主体（roles=staff）、同一 scope（internal_product）。

## 3. 语料与评测集（必须诚实标注的部分）

- **语料**：本仓库 18 篇 Markdown（`docs/**` + `README.md` + L1 架构契约 40KB），
  入库得到 **181 个 child 切片**，全部写入 BGE-M3 向量（1024 维）。
- **评测集**：`_evidence/recall_cases_20261104.json`，**50 条**中文问题，每条带
  `needle`（人工挑选、脚本校验确实存在于语料）与 `doc`（所属文档）。
- **偏置**：问题是本项目的实施者写的，**不是客户问题**；语料是工程文档，不是玻璃仪器/工艺语料。
  因此：**数字只用于「同一语料下三种召回方式的相对比较」，不能当作客户场景的验收值。**
  真正的验收要按 `docs/SPEC/embedding-and-hybrid.md` §6 用客户真实资料与人工标注重跑。

## 4. 复现

```bash
# 一次性：装可选依赖（torch CPU + sentence-transformers，约 300MB）
uv sync --all-groups --extra embedding

# 权重（约 2.3GB）：本机 HuggingFace 直连失败，用镜像 + 工作区内缓存
$env:HF_ENDPOINT="https://hf-mirror.com"
$env:HF_HOME="H:/MyWork/Factory-Knowledge-Hub/.hf-cache"

uv run python -m fkh.tools.recall_benchmark \
  --corpus docs README.md "Factory-Knowledge-Hub-V1.0-L1-架构与开发契约-Rev2-含新人培训.md" \
  --cases _evidence/recall_cases_20261104.json \
  --k 10 --pgdata "$env:TEMP/fkh-pgdata" \
  --output _evidence/recall_report_20261104.json
```

逐条结果（含每条的 top-10 文本）在 `_evidence/recall_report_20261104.json`。
OR 变体是**实验**而非产品行为：直接用 `to_tsquery('simple', '字 | 字 | …')` 在评测库上跑同一批题。

## 5. 环境与已知限制

- 本机 `torch 2.15.0+cpu`、`sentence-transformers 6.2.0`、`transformers 5.22.1`；模型 `BAAI/bge-m3`（MIT，1024d）。
- **CPU 推理**：入库 181 个切片约 1–2 分钟；单条查询 ~60ms（含 SQL + 融合 + 两次取文）。
  这是 CPU 数字，上 GPU 只会改善查询延迟，不改变召回质量。
- 权重缓存必须放在**工作区内**（`.hf-cache/`，已 gitignore）：本机子进程写 `%TEMP%`/`D:`/`E:` 会被拒
  （`WinError 5`），HuggingFace 直连也连不上，只能用 `hf-mirror.com`。
- `BGEM3Embedder` 用 `local_files_only=True`，离线可用；缺权重时报错而不是偷偷联网。
- 未做 rerank、未做多语种评测、未测客户真实语料；`n=50` 的置信区间很宽（±10pp 量级），
  只适合看方向，不适合卡阈值。

## 6. 建议（按性价比排序）

1. **先修词法查询构造**（最高性价比，无模型依赖）：自然语言问句不应把每个汉字都 AND 起来。
   改成「OR 检索 + `ts_rank_cd` 排序」或 `websearch_to_tsquery`，预期把词法从 0.06 抬到 0.66 量级。
   这会改变 `POST /v1/knowledge/search` 的返回集合（对外可见行为）→ **按对外契约变化处理，先送 L1 并补断言**。
2. **再决定混合是否默认开启**：修完词法后重跑本评测，看混合相对 OR-词法的净增益是否仍值得
   34 倍延迟与模型运维成本。当前数据（+14pp）支持"生产可选开启"，不支持"默认开启"。
3. **客户语料验收**：用 50–100 条真实问题 + 人工标注正确片段重跑；低于词法基线就不该引入模型
   （`embedding-and-hybrid.md` §6 的原文要求仍然有效）。
4. 混合模式继续保持**服务端 opt-in、生产未授权**（L1 Q2）；本次实测不改变该结论。
