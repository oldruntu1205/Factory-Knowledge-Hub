# API 调用手册（照着抄就能跑）

> 面向：要对接本系统的 ERP / MES / 内部工具开发者。
> 契约以 `contracts/openapi/openapi.yaml` 为单一来源；本文件是**可执行的示例**，不替代契约。
> 所有示例在本地演示环境（`http://127.0.0.1:8000`）真实跑过，响应片段取自实测输出。

## 0. 三件必须先知道的事

1. **路径里的 `{knowledge_id}` 实际是 `version_id`**（历史命名）。新的文档接口返回 `version_id`，
   审核/发布/撤下都用它，不要传 `document_id`。
2. **入库是 `DRAFT`，不可检索**。必须 `review(PASS)` → `publish(internal)` 才进入检索；
   对外可见还要 `publish(customer_public)`，且满足：已是 `APPROVED_INTERNAL`、
   **分级 C0**、公开审批人 ≠ 技术审批人。
3. **引用 id 是上下文块 id**：父块恢复时它就是父块 id。真实存在但没进入该轮上下文的块 id
   会被引用白名单丢弃（等价于没引用）。

## 1. 认证

所有 `/v1/**` 需要 `Authorization: Bearer <JWT>`。令牌至少包含：
`iss/aud/sub/iat/exp/tenant_id/principal_id/roles/scope/allowed_scopes`。

- `scope`：能做什么（如 `knowledge.ingest`、`knowledge.review`）
- `allowed_scopes`：能看哪个数据范围（`internal_product` / `internal_training` / `customer_public`）

两者**分别校验**：有 `knowledge.review` 但 `allowed_scopes` 不含该版本范围，一样看不到。

```bash
TOKEN='<你的 JWT>'
API='http://127.0.0.1:8000'
AUTH="Authorization: Bearer $TOKEN"
```

## 2. 上传文档（`knowledge.ingest`）

正文走 JSON + base64，**解码后 ≤ 5 MiB**，HTTP 请求体上限 8 MiB（超出 413 `PAYLOAD_TOO_LARGE`）。

```bash
B64=$(base64 -w0 quality.md)          # macOS: base64 -i quality.md
curl -sS -X POST "$API/v1/knowledge/documents" \
  -H "$AUTH" -H 'Content-Type: application/json' \
  -d "{
    \"filename\": \"quality.md\",
    \"title\": \"质量检验说明\",
    \"domain\": \"quality\",
    \"owner_department\": \"qc\",
    \"classification\": 2,
    \"audience\": \"internal\",
    \"required_scope\": \"internal_product\",
    \"acl_allow_roles\": [\"staff\"],
    \"acl_deny_roles\": [],
    \"knowledge_key\": \"quality-check\",
    \"content_base64\": \"$B64\"
  }"
```

实测响应（201）：

```json
{
  "document_id": "5c590a1f-12c2-48e8-82f8-bdf38f2ccf3e",
  "version_id": "cd38bb63-4568-4f73-be40-91a4e8277c62",
  "version_no": 1,
  "status": "DRAFT",
  "parent_chunks": 1,
  "child_chunks": 1,
  "needs_human_review": false,
  "warnings": ["parent#0 仅 169 tokens，低于下限 800（标题分段造成，可接受但需抽查）"]
}
```

要点：
- `knowledge_key` 同租户唯一，重复 → **409**（不覆盖、不新建版本）；
- `product_catalog` 域请带 `catalog_currency`（如 `"CNY"`），否则不抽目录条目；
- 失败不留半成品：文档/版本/切片/ACL/目录条目同事务。

## 3. 列表（`knowledge.read`）

```bash
curl -sS "$API/v1/knowledge/documents?status=DRAFT&limit=50" -H "$AUTH"
```

```json
{"total": 1, "limit": 50, "offset": 0, "items": [{
  "document_id": "5c590a1f-...", "knowledge_key": "quality-check",
  "title": "质量检验说明", "domain": "quality", "owner_department": "qc",
  "version_id": "cd38bb63-...", "version_no": 1, "status": "DRAFT",
  "classification": 2, "audience": "internal",
  "required_scope": "internal_product", "created_at": "2026-11-07T06:37:27+00:00",
  "chunk_count": 2}]}
```

- 顺序固定：租户 → 取每个文档**最新版本** → 数据范围/状态过滤 → 排序分页；
- 最新版本不可见时整条文档消失，**不回退旧版本**。

## 4. 审核与发布

```bash
VID='<version_id>'

# 通过技术审核：DRAFT → REVIEWED
curl -sS -X POST "$API/v1/knowledge/$VID/review" -H "$AUTH" \
  -H 'Content-Type: application/json' -d '{"decision":"PASS"}'

# 内部发布：REVIEWED → APPROVED_INTERNAL（可被检索）
curl -sS -X POST "$API/v1/knowledge/$VID/publish" -H "$AUTH" \
  -H 'Content-Type: application/json' -d '{"audience":"internal"}'

# 对外发布：APPROVED_INTERNAL → PUBLISHED_PUBLIC（需 publish_public + 另一主体 + C0）
curl -sS -X POST "$API/v1/knowledge/$VID/publish" -H "$AUTH_PUBLISHER" \
  -H 'Content-Type: application/json' -d '{"audience":"customer_public"}'
```

驳回：`{"decision":"REJECT","reason":"格式不符合目录规范"}` → `DISCARDED`。

对外发布的失败语义（**先权限、后状态**，两个阶段都可能拒）：

| 阶段 | 情形 | 返回 |
|---|---|---|
| 权限 | 令牌缺 `knowledge.publish_public` | 403（**先于**状态判断） |
| 状态 | 见下表 | 409 |

| 情形 | 返回 |
|---|---|
| 还不是 `APPROVED_INTERNAL` | 409 |
| `classification != 0` | 409（仅 C0 可公开） |
| 公开审批人 == 技术审批人 | 409（四眼原则） |

## 5. 撤下与作废（`knowledge.revoke`）

```bash
# 撤下：APPROVED_INTERNAL / PUBLISHED_PUBLIC → REVOKED，立即不可检索
curl -sS -X POST "$API/v1/knowledge/$VID/revoke?reason=$(python -c 'import urllib.parse;print(urllib.parse.quote("内容过时"))')" -H "$AUTH"

# 作废：DRAFT / REVIEWED → DISCARDED（表示从未公开过）
curl -sS -X POST "$API/v1/knowledge/$VID/discard?reason=上传有误" -H "$AUTH"
```

实测：撤下后同一秒再问，客户视角立刻 `REFUSED / NO_EVIDENCE`；审计表 `review_actions`
出现 `REVOKE` 记录（含 actor 与 reason）。

## 6'. 实测记录（2026-11-07，本地演示环境）

本文件里的命令在本机逐条跑过，关键结果：

```
上传                → 201 DRAFT（version 91a2ee7d…）
审核 PASS           → REVIEWED
内部发布            → APPROVED_INTERNAL
问答「资料里有什么要求？」→ ANSWERED，1 条引用
重复 knowledge_key  → 409 VERSION_CONFLICT
C2 内容对外发布（无 publish_public）→ 403
C2 内容对外发布（有 publish_public）→ 409（仅 C0 可公开）
```

## 6. 问答（`knowledge.answer`）

```bash
curl -sS -X POST "$API/v1/knowledge/answer" -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"query":"气密性测试要充气到多少压力？","knowledge_scope":"internal_product",
       "language":"zh-CN","max_evidence":5}'
```

关键响应字段：

```json
{
  "status": "HUMAN_REVIEW_REQUIRED",
  "refusal_code": "UNSUPPORTED_CLAIM",
  "answer": "测试压力通常为设备工作压力的 1.5 倍或更高…",
  "citations": [{"evidence_id": "237a4c04-…", "knowledge_id": "…", "version_id": "…", "locator": {}}],
  "literal_support": {
    "rule": "literal-support-v4",
    "applicable": true,
    "supported": false,
    "numbers": {"total": 1, "supported": 0, "unsupported": ["1.5倍"]},
    "codes": {"total": 0, "supported": 0, "unsupported": []},
    "disclaimer": "仅表示所覆盖的货号/带单位数值在被引用的证据中字面出现；……"
  }
}
```

状态怎么读：

| status | 含义 | 调用方该做什么 |
|---|---|---|
| `ANSWERED` | 有证据、引用合法、数值受支持 | 可直接展示 |
| `HUMAN_REVIEW_REQUIRED` | 答案保留但需要人看（如数值未被支持） | 转人工，**不要直接给客户** |
| `REFUSED` | `NO_EVIDENCE` / `INSUFFICIENT_EVIDENCE` | 说“资料未说明”，不要补常识 |
| HTTP 503 | 推理服务不可用 | 重试；不要显示成“不会回答” |

`literal_support` 的边界（重要）：通过 ≠ 语义一致；未通过 ≠ 一定错（可能是改写或单位换算）。

## 7. 证据原文（`evidence.read`）

```bash
curl -sS "$API/v1/evidence/237a4c04-…" -H "$AUTH"
```

- 父块 / 子块 id 都能查；返回体里 `text_level` 表明实际正文来源，`text_evidence_id` 是提供正文的块；
- 拒绝一律 **403**（不存在 / 跨租户 / 状态不可见 / scope 不符 / ACL 拒绝，同码同文案）；
- `signed_url` 在 ADR-01 签署前固定 `null`，只有文本。

## 8. 目录冲突待办（`catalog.read` / `catalog.resolve`）

```bash
# 列表（默认只看 OPEN）
curl -sS "$API/v1/catalog/conflicts?status=OPEN&limit=50" -H "$AUTH"

# 决议：二选一（KEEP 必须给 candidate_id）
curl -sS -X POST "$API/v1/catalog/conflicts/<group_id>/resolve" -H "$AUTH" \
  -H 'Content-Type: application/json' -d '{"action":"KEEP","candidate_id":"<candidate_id>"}'

# 或：该货号不设条目（不能同时给 candidate_id）
curl -sS -X POST "$API/v1/catalog/conflicts/<group_id>/resolve" -H "$AUTH" \
  -H 'Content-Type: application/json' -d '{"action":"DROP_ALL"}'
```

决议**一次性生效、不可改判**（重复决议 → 409）。错判请走新版本。

## 9. 错误响应（统一形状）

```json
{"error": {"code": "VALIDATION_ERROR", "message": "Request payload is invalid (domain: not one of the allowed values)", "trace_id": "…"}}
```

- 422/413 的 message 会带「字段: 规则」，**不含**你提交的原始值；
- 401/403/404 保持固定文案（不区分“不存在”和“无权限”，避免探测）；
- 排查请用 `trace_id` 关联服务端日志与审计表。

## 10. 一份最小可跑的端到端脚本

```bash
#!/usr/bin/env bash
set -euo pipefail
API=http://127.0.0.1:8000; AUTH="Authorization: Bearer $TOKEN"
B64=$(base64 -w0 sample.md)
VID=$(curl -sS -X POST "$API/v1/knowledge/documents" -H "$AUTH" -H 'Content-Type: application/json' \
  -d "{\"filename\":\"sample.md\",\"title\":\"示例\",\"domain\":\"quality\",\"owner_department\":\"qc\",
       \"classification\":2,\"audience\":\"internal\",\"required_scope\":\"internal_product\",
       \"knowledge_key\":\"sample-$RANDOM\",\"content_base64\":\"$B64\"}" | python -c 'import json,sys;print(json.load(sys.stdin)[\"version_id\"])')
curl -sS -X POST "$API/v1/knowledge/$VID/review"  -H "$AUTH" -H 'Content-Type: application/json' -d '{"decision":"PASS"}' > /dev/null
curl -sS -X POST "$API/v1/knowledge/$VID/publish" -H "$AUTH" -H 'Content-Type: application/json' -d '{"audience":"internal"}' > /dev/null
curl -sS -X POST "$API/v1/knowledge/answer" -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"query":"资料里有什么要求？","knowledge_scope":"internal_product","language":"zh-CN","max_evidence":5}'
```
