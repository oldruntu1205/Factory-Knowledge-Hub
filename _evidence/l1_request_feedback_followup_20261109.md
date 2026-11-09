# L1 复送：`/v1/feedback` 输入面收紧（回应 CHANGES_REQUESTED）

- 日期：2026-11-09
- 上一轮回执：`_evidence/l1_reply_feedback_20261109.md`
- 预算：本切片第 2 次 L1 调用（上限 3 次）。

## 一、MUST_FIX 的修复

### 1) `expected_behavior` 改为封闭结构，不再接受任意 JSON

```python
class ExpectedBehavior(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_status: Literal["ANSWERED", "REFUSED", "HUMAN_REVIEW_REQUIRED"]
    must_cite: bool = False
```

- 只有两个字段：枚举 + 布尔；**没有任何自由文本/嵌套对象**；
- 未知字段（如 `notes`、`should_refuse`、`tenant_id`）一律 422；
- 嵌套深度固定为 1 层，序列化大小由请求体上限兜底；
- OpenAPI 已同步为 `additionalProperties: false` + `required: [expected_status]` + 枚举。

### 2) 请求体硬上限

`main.py` 的接收层限流从"只对上传"扩展为路径表：

```python
MAX_FEEDBACK_BYTES = 16 * 1024
_BODY_LIMITS = {
    "/v1/knowledge/documents": 8 * 1024 * 1024,
    "/v1/feedback": MAX_FEEDBACK_BYTES,
}
```

- 先查 `Content-Length`，再逐块累计；超过 16 KiB 立即 413，不缓冲完整包；
- 上传路径行为不变；
- `expected_behavior` 只含标量，16 KiB 足够且远小于 PII 批量写入的可用空间。

### 3) 新增测试

- `expected_behavior` 带 `notes`（自由文本）→ 422；
- 请求体 16 KiB + 1 字节 → 413；
- 未知顶层字段 → 422；
- 成功后 `bad_cases.expected_behavior` 等于 `{"expected_status":"REFUSED","must_cite":false}`。

定向结果：`pytest tests/e2e/test_feedback_api.py tests/contract/test_api_contract.py` → **19 passed**；ruff/mypy 全绿。全量 suite 合并前跑。

## 二、SHOULD_FIX 的处理/说明

- **授权范围**：接受 L1 判断——`feedback.create` 是租户级登记权限，可关联本租户任意主体的问答；`redacted_case` 仍只存 trace/hash/category，不含原文。会在代码 docstring/交付说明中写明该权限语义。
- **404**：保持不存在/跨租户同 404、固定响应体；OpenAPI 已列 404/422。
- **P2**：保持固定默认，响应中明确返回 `severity: "P2"`。
- **重复提交**：接受 L1 判断，允许重复登记；不加唯一索引。
- **outbox**：本轮不接线；在交付说明中登记"`rag.bad_case.created.v1` 未发事件"为待办，不声称已闭环。
- **query_hash 非匿名化**：不把它当扩大授权的理由；授权仍由 `feedback.create` 决定。

## 三、请 L1 判断

1. 封闭 `ExpectedBehavior`（仅 `expected_status` + `must_cite`）是否足以解除 MUST_FIX？
2. 16 KiB 请求体硬上限 + 固定深度/标量字段，是否满足"大小/深度/序列化上限"要求？
3. 其余项按上述边界处理，是否可 PASS？若有剩余 MUST_FIX，请给最小修复。

## 四、固定回复骨架

```text
VERDICT: PASS | CHANGES_REQUESTED

MUST_FIX:
- <问题>: <为什么阻塞> -> <最小修复>

SHOULD_FIX:
- <问题>: <建议>

BOUNDARY:
- <本轮不要求覆盖的项>
```
