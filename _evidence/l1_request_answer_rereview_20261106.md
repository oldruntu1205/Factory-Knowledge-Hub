# L1 复审：问答切片的两项证据闭合（`7151bd5`）

- 日期：2026-11-06
- 对应：`_evidence/l1_reply_answer_final_20261106.md`（CHANGES_REQUESTED 2 项）
- 请只做判断：**不要探索仓库、不要读源码、不要跑测试**。

## 1. 第 1 项（#5）：连接绑定已校验的 IP

**你的质疑成立。** 原实现保存的是主机名（`localhost`），`http.client` 建连时会再次解析——
校验的地址与连接的地址可能不是同一个（hosts 改写 / DNS rebinding 可绕过）。

改法：`_resolve_loopback()` 现在**返回数值 IP**（要求所有解析结果均为 loopback，优先 IPv4），
构造函数把它存为连接目标；`HTTPConnection(IP, port)` 不再接收主机名。

回归证据 `tests/unit/test_generation_client.py::test_connection_binds_validated_ip_and_never_re_resolves`：

1. 用真实回环 HTTP 服务 + 计数版 `socket.getaddrinfo`；
2. 断言 `generator._host` 可被 `ipaddress.ip_address()` 解析且 `is_loopback()`——即连接目标是 IP 字面量；
3. 断言**首次**解析的输入是 `localhost`（校验），之后**不再出现主机名**解析
   （后续只有对 `127.0.0.1` 这一字面量的内部建连解析，IP 字面量的解析结果不会变）；
4. 用替换的 `socket.create_connection` 记录实际建连参数，断言其 `host == generator._host`，
   且不是 `"localhost"`。

配套仍保留的边界测试：非 loopback 字符串直接拒绝；`localhost` 解析到 `8.8.8.8` 时拒绝；
302 只请求一次不跟随；设置 `HTTP_PROXY` 仍直连本机；模型未安装时拒绝且不 `pull`。

## 2. 第 2 项（#8）：父块恢复的越权内容

新增 `tests/e2e/test_answer_api.py::test_parent_restore_excludes_unauthorized_parent_content`：

- 版本 `APPROVED_INTERNAL`（scope=internal_product）；
- **父块** ACL 只允许 `manager`，正文含秘密串「内部成本价 3.5 元，禁止外传。」；
- **子块** ACL 允许 `staff`（本用例主体只有 `staff` 角色），正文为可检索内容，挂在那个父块下；
- 提问后命中该子块。

断言（全部通过）：

1. `status == ANSWERED`（说明确实走了正常作答路径，不是"整条被拒"掩盖越权）；
2. 送进生成器的完整提示词中**不含**「内部成本价」，也不含整段秘密串；
3. 返回引用只有子块 ID，父块 ID 不在引用里。

即：`plan_restore` 的既有权衡（父块或任一兄弟块不可读 ⇒ 只回退子块文本）在问答路径上有直接回归证据。

## 3. 额外真实取样（非 mock，供参考）

本机真实 Ollama（`qwen3:14b`）+ 真实 PostgreSQL 跑通：

```
model_id = qwen3:14b@bdbd181c33f2
status   = ANSWERED
answer   = 冷凝管的标准接口为 24/29 与 29/32 [fb449fda-...db7]。
无证据请求：REFUSED / NO_EVIDENCE，耗时 0.0ms（证明未调用生成）
命中请求：约 4.8s
```

记录见 `_evidence/answer_real_sample_20261106.md`。

## 4. 门禁（独家运行）

```
uv run pytest -q                      → 431 passed, 62.13s
uv run pytest -q -m requires_pg       → 266 passed, 165 deselected, 0 skip
uv run ruff check src tests migrations → All checks passed
uv run mypy                           → Success: no issues found in 56 source files
```

请裁决：**PASS** / **仍 CHANGES_REQUESTED（仅列未满足点）**。
