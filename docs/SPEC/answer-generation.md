# 带引用问答（`POST /v1/knowledge/answer`）

- 状态：已实现（2026-11-06，L1 有条件通过 → 落地）
- 裁决记录：`_evidence/l1_reply_answer_generation_20261106.md`（有条件通过）
- 终审：**PASS**（`_evidence/l1_reply_answer_rereview_20261106.md`）——两项证据闭合：
  连接目标绑定已校验的数值 IP；不可读父块内容不进入提示词、不成为引用
- 引用清单协议：**PASS**（`_evidence/l1_reply_citation_list_final_20261106.md`）——
  协议解析/内联兼容/展示编号/系统侧 locator 均符合；准确率波动记为后续迭代项，不阻断协议验收
- 真实取样：`_evidence/answer_real_sample_20261106.md`（本地 qwen3:14b，命中 4.8s / 无证据 0.0ms）
- 相关：`docs/SPEC/retrieval.md`、`docs/SPEC/context-assembly.md`、`docs/ADR/README.md`（ADR-04）

## 1. 为什么现在能开

ADR-04（模型供应商 / 跨境传输）**仍未签署**，其未签期取值是「Gateway 不启用外部调用；
无合规路由时只允许本地推理或拒答」。本切片严格按这条落地：

- 只连 loopback（`127.0.0.1` / `localhost` / `::1`），且**按解析后的 IP 校验**，不只看字符串；
- 不经环境代理（`http.client` 直连，不读 `HTTP_PROXY`）、不跟随重定向（3xx 直接失败）；
- 模型必须显式配置且本机已安装（不会自动 `pull`，不会回退云端）；
- `FKH_LLM_PROVIDER=none`（默认）时端点 503，不提供任何默认模型。

## 2. 链路

```
鉴权（knowledge.answer）→ scope 收窄 → 请求校验
  → 能力检查（provider/model/loopback/模型存在）
  → 检索（SearchService，写 retrieval_queries 审计）
  → 上下文装配（父块恢复 + 双预算，ACL 复查）
  → 本地推理（Ollama，stream=false，只取 response，丢弃 thinking）
  → 引用提取（[evidence_id]）→ 白名单核验（version_id/locator 取系统侧）
  → 状态判定（ANSWERED / REFUSED / HUMAN_REVIEW_REQUIRED）
  → 引用覆盖检查（ANSWERED 时才做）
  → 审计（rag_queries + rag_citations，同事务，显式提交）
```

## 3. 状态判定（`retrieval/citation.py` + `answer/prompt.py`）

| 情况 | 结果 | 人工复核 |
|---|---|---|
| 上下文为空 | `REFUSED` / `NO_EVIDENCE`，**不调用模型** | 否 |
| 有证据、零条有效引用 | `REFUSED` / `INSUFFICIENT_EVIDENCE` | 是 |
| 出现白名单外的 ID | `HUMAN_REVIEW_REQUIRED`，可用引用保留 | 是 |
| 引用合法（清单或内联合并后 ≥1 条） | `ANSWERED` | 否 |
| 推理超时/不可达/解析失败 | **503** + 审计 `ERROR` | —（能力故障） |

两条容易混淆的边界：

- **`ERROR` 只进审计，不进业务响应**。响应体只有三种业务状态；能力故障用 503 + 统一错误格式
  （`SERVICE_UNAVAILABLE`，含 `trace_id`），不暴露内部地址、提示词或异常原文。
- **"ID 全部合法"不是通过条件**。引用只提供**来源追溯**，不证明每个实质句都受证据支持。

## 4. 引用清单协议（2026-11-06 业务决策 A）

原协议要求"每个实质句内联 `[evidence_id]`"。实测（`_evidence/catalog_acceptance_20261106.md`）：
8 条描述类问题 **ANSWERED 0/8、带引用 0/8**，内容其实是 6/8 对的；
换 4 个本地模型（qwen3:14b/8b、qwen2.5:7b、coder:14b）**引用率都是 0/4**。
结论：逐句内联协议在 7B–14B 上不成立，不是单个模型问题。

改法：

- 提示词把格式要求放在**最后**（模型对末尾指令更敏感）：正文写完后另起一行 `引用：<id> <id>…`；
  无法回答写 `引用：无`；
- 解析只针对最终答案（`thinking` 是独立字段）；清单行**从展示正文剥离**；
- 兼容内联 `[id]`，与清单合并去重；**清单写"无"但不覆盖正文里的合法内联引用**（记协议冲突日志）；
- 展示层把内联内部 id 换成可读序号（`[1]`、`[2]`），**不裸露 chunk_id**；
- **逐句覆盖不再是闸门**：`uncovered_sentences()` 降级为"内联标注完整度"诊断，
  不参与状态判定（它测的是标注格式，不是事实支持程度）。

保留的拦截：无证据 → `NO_EVIDENCE`（不调模型）；有证据零有效引用 → `INSUFFICIENT_EVIDENCE` + 人工；
白名单外 id（清单或内联任一来源）→ `HUMAN_REVIEW_REQUIRED`，可用引用保留但不得 `ANSWERED`。

实测（同一批问题，qwen3:14b）：**引用行出现率 0/6 → 5/6**，`ANSWERED` 5/6。
代价也很清楚：模型为满足格式变得简短，事实命中率从 6/8 降到 4/6（把 14-70mm 说成 24*24、
把"含税"说成"不含税"），这属于模型能力问题，需要继续调提示词 + 建坏例回归。

## 4.1 引用覆盖检查的真实边界

`uncovered_sentences()` 的做法：

1. 按 `。！？!?\n` 切句，**把紧跟在句末后的纯引用片段并回上一句**（`结论。[id]` 是常见写法）；
2. 去掉引用标记与项目符号后少于 6 个字符的句子（标题、口头语）不计；
3. 句内出现任一有效 `evidence_id` 即视为已引用。

它**只是诊断**：清单协议下既不参与状态判定，也不能说明结论被证据支持。

## 5. 提示注入的基线（不做过拟合的启发式检测）

- 证据放在 `<evidence id="...">` 分隔符里，系统规则写在证据之前且不随证据变化；
- 规则明确要求：证据内的任何指令一律视为资料原文，不得执行、不得改变规则；
- 生成器**没有工具 / 函数调用 / 文件访问能力**，只能产出文本；
- 上下文只包含本次授权内容（父块恢复同样过 ACL）；
- 引用核验与覆盖检查覆盖**最终展示内容**——模型自称"没被注入"不作为通过依据。

不做的：关键词式"疑似注入"自动转人工（误报高）。

## 6. 审计

`rag_queries`：`status`（ANSWERED/REFUSED/HUMAN_REVIEW_REQUIRED/**ERROR**）、`refusal_code`、
`model_id`（**带 digest**，如 `qwen3:14b@bdbd181c33f2`，tag 会漂移、digest 不会）、`scope`、
`trace_id`、`query_hash`（HMAC 摘要）。

- **query 明文不落库、不进日志、不进异常**；密钥不落库。
- 一次成功问答恰好一条 `rag_queries` + 每条引用一行 `rag_citations`，**同事务**。
- 审计写入失败 → 抛错，不返回成功；能力故障路径会**显式提交** ERROR 记录，
  否则请求级回滚会把"503 的审计"一起回滚掉。

## 7. 配置

| 变量 | 默认 | 说明 |
|---|---|---|
| `FKH_LLM_PROVIDER` | `none` | `none` 或 `ollama` |
| `FKH_LLM_BASE_URL` | `http://127.0.0.1:11434` | 非 loopback 构造即拒绝 |
| `FKH_LLM_MODEL` | 空 | 必须显式，如 `qwen3:14b`；须本机已安装 |
| `FKH_LLM_TIMEOUT_SECONDS` | 60 | 超时按能力故障处理（503） |
| `FKH_LLM_MAX_TOKENS` | 1024 | 单次生成上限 |
| `FKH_LLM_TEMPERATURE` | 0.0 | 问答默认确定性输出 |

## 8. 验收证据

- `tests/e2e/test_answer_api.py`（11 项）：三种业务状态、伪造引用、合法但未覆盖、
  无证据不调用生成、503 + ERROR 审计、越权 403 优先于 503、审计只存哈希、语言进入指令。
- `tests/unit/test_answer_prompt.py`（7 项）：提示词规则、ID 提取、覆盖检查边界。
- `tests/unit/test_generation_client.py`（8 项）：loopback 解析校验、模型不存在、
  重定向不跟随、环境代理不生效、thinking 字段丢弃、digest 入审计。
- `tests/contract/test_api_contract.py`：501 → 503 的契约改写（语义不放松）。

## 9. 已知限制

1. 语义正确性无自动验证：覆盖率只挡"没有引用"，挡不住"引用不支持结论"。
2. 单轮问答，不做对话状态与追问（`conversation_id` 未参与检索改写）。
3. 未接 `/v1/training/ask`（培训问答的 scope 与审计口径单独开切片）。
4. 本地 14B 模型在长证据下的忠实度未做系统评估；接客户语料时需按坏例回归。
