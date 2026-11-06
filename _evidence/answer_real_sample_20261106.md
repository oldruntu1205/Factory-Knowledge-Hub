# 真实取样：本地 qwen3:14b 跑通 `/v1/knowledge/answer`（2026-11-06）

不是 mock：真实 PostgreSQL（本机 pgserver）+ 真实 Ollama（`127.0.0.1:11434`）+ 真实检索与审计。

## 1. 命中路径

```
model_id = qwen3:14b@bdbd181c33f2
status = ANSWERED
answer   = 冷凝管的标准接口为 24/29 与 29/32 [fb449fda-4616-47e2-8b0d-d8ac67364db7]。
citations = ['fb449fda-4616-47e2-8b0d-d8ac67364db7']
chunk_id  = fb449fda-4616-47e2-8b0d-d8ac67364db7
refusal_code = None
```

模型自己写了 `[chunk_id]`，系统按白名单把它转成受控引用；`version_id`/`locator` 取自系统侧。

## 2. 无证据路径（不调用模型）

```
miss: status=REFUSED code=NO_EVIDENCE 0.0ms
hit : status=ANSWERED code=None 4797.0ms
```

- 无证据时耗时 **0.0ms**：说明生成确实没有被调用（不是"调用后拒绝"）。
- 命中一次约 **4.8s**（qwen3:14b，本机 14B Q4；不含首次模型加载）。
- 同一批请求里，第二次回答的引用 ID 与数据库里的 chunk 完全一致。

## 3. 这组取样证明了什么、没证明什么

证明：本地推理链路（检索 → 上下文 → 推理 → 引用核验 → 审计）在真实模型上可跑通，
引用能被正确解析并落成系统侧受控引用。

没证明：语义忠实度（引用是否支持结论）、长证据下的稳定性、并发下的吞吐。
这些需要客户语料与坏例回归，见 `docs/SPEC/answer-generation.md` §9。
