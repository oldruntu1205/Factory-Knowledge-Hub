# 本地私域演示环境

一条命令起一整套「内嵌 PG + API + 演示页」，形态就是客户内网的样子：全部只绑 `127.0.0.1`，
不需要域名、不需要外网。

```powershell
# 推荐：一键脚本（会先预检/拉起 Ollama，并检查 8000/8010 是否被旧进程占用）
powershell -ExecutionPolicy Bypass -File scripts\demo-up.ps1

# 或者手动
uv run python -m fkh.tools.demo_app --provider ollama --model qwen3:14b
```

启动后：

| 入口 | 地址 | 说明 |
|---|---|---|
| 演示页 | http://127.0.0.1:8010/ | 给人看的入口：提问、看状态/答案/引用/原始响应 |
| API | http://127.0.0.1:8000/ | OpenAPI 契约里的真实服务 |
| 数据库 | 127.0.0.1:55432 | 内嵌 PG 16 + pgvector，数据落 `.demo-pgdata/` |

演示页支持 `?q=...` 直接带问题打开（截图/演示用）。

## 它做了什么（幂等，可重复启动）

1. 首次运行 `initdb` 初始化 `.demo-pgdata/`（已存在则复用）；
2. `pg_ctl` 启动 PG（**不走 pgserver.get_server**：它只等 10 秒，本机首次恢复可能 30 秒以上）；
3. 应用全部迁移 `0001–0017`；
4. 播种「三爱思演示」租户：入库目录 → 审批 `APPROVED_INTERNAL` → 抽取目录条目与冲突待办；
   已存在则跳过；
5. 起 API（同进程线程）与演示页（内置 http.server），浏览器里不出现任何令牌——
   JWT 由演示页服务端代持。

## 演示脚本（建议按这个顺序讲）

| 步骤 | 提问 | 预期 | 说明的点 |
|---|---|---|---|
| 1 | `A112420 的单价是多少？` | `ANSWERED`，带 1 条引用，毫秒级 | 结构化通道：确定性、不经过模型 |
| 2 | `A999999 的单价是多少？` | `REFUSED / NO_EVIDENCE` | 不编造不存在的货号 |
| 3 | `冷凝管的标口范围是多少？` | `ANSWERED` 或 `HUMAN_REVIEW_REQUIRED`（首次调用较慢） | 描述类走了检索 + 本地模型；不确定就转人工 |
| 4 | `你们的产品能开发票吗？` | `REFUSED / NO_EVIDENCE` | 语料没有的内容不猜 |

页面底部「原始响应」可以直接展示 `status / refusal_code / citations / unsupported` 等审计字段。

## 冲突待办页（业务前端第一片）

页面直接接 `GET /v1/catalog/conflicts` 与 `POST …/{id}/resolve`（JWT 由演示服务端代持，浏览器里没有令牌）。

演示顺序（实测一遍约 1 分钟）：

1. 打开 `/conflicts`，顶部显示「待办组 29 / 候选行 62」；
2. 看 `A112440`：两条候选 `57.00` 与 `56.00`，规格不同——这就是要人判的冲突；
3. 点候选旁的「原文」→ 弹出 `GET /v1/evidence/{chunk_id}` 返回的证据原文（本例是第 5 页整页，
   `text_level=PARENT`），运营可对着原文判断 57 还是 56；
4. 点其中一条的「选这个」→ 卡片消失、计数变 28/60，提示「已保留该候选」；
5. 回到问答页问 `A112440 的单价是多少？` → 立刻 `ANSWERED`，返回刚选的价格；
6. 再点一次同一组的决议接口会得到 409（不可改判），页面已无该卡片；
7. 回问答页点答案下方的「[1] 查看原文」→ 同一个弹层打开该引用所在页，版本/状态/章节置顶常显。

`全部丢弃` 表示该货号不设条目（需二次确认）；决议一次性生效，错判走新版本。

## 撤下与作废（立即生效）

文档审核页里每个已生效的版本都有「撤下（立即失效）」；`DRAFT`/`REVIEWED` 有「作废」。

演示顺序：

1. 任一 `APPROVED_INTERNAL` / `PUBLISHED_PUBLIC` 文档点「撤下」，填原因；
2. 列表状态变 `REVOKED`；同一瞬间再去问，内部与客户视角都检索不到（无需等缓存）；
3. 审计表 `review_actions` 里出现 `REVOKE` 记录，含操作者与原因。

实测（本机演示库）：撤下客户版说明后，客户视角问「售后保修期」→ `REFUSED / NO_EVIDENCE`。

## 客户视角（对外发布链路）

演示环境有**两套令牌**，切换的是服务端授权面，不是前端开关：

| 视角 | 令牌 scope | 数据范围 |
|---|---|---|
| 内部视角 | answer/search/review/publish/ingest/catalog… | `internal_product` + `customer_public` |
| 客户视角 | 仅 answer/search/evidence.read（**无审核、无发布**） | 只有 `customer_public` |

演示顺序：

1. 文档审核页上传一份 `audience=customer_public`、**分级 C0** 的说明文档（非 C0 不得对外发布）；
2. 通过审核 → 发布（内部可用）→ 出现「发布为对外（客户可见）」，点它；
   这一步由**第二个主体**执行（四眼原则：公开审批人不得与技术审批人同一）；
3. 回问答页切到「客户视角」问售后保修 → ANSWERED，引用来自刚才那份文档；
4. 客户视角问内部资料（如 `A112420 的单价`）→ `REFUSED / NO_EVIDENCE`，**不会泄漏内部价格**。

## 文档审核页（业务前端第二片）

页面接 `POST/GET /v1/knowledge/documents` 与既有 review/publish（JWT 由演示服务端代持）。

演示顺序（实测约 2 分钟，含一次真实问答）：

1. 选一个文本文件（如 `_corpus/` 里的说明文档），填标题、业务域、范围；
2. 点「上传并入库」→ 提示 `DRAFT` 与切片数，列表里出现该文档（待审视图）；
3. 点「通过审核」→ 状态变 `REVIEWED`，按钮换成「发布（内部可用）」；
4. 点「发布」→ `APPROVED_INTERNAL`，此时才可被检索；
5. 回问答页问该文档里的问题 → 有引用；点引用看原文。

注意：`DRAFT` 状态下搜索不到是**正确行为**（审核未过）。

## 停止

前台运行时 `Ctrl+C`：会先停 API、再 `pg_ctl stop -m fast` 停数据库。
若窗口被强关，下一次启动前清理残留：

```powershell
Get-CimInstance Win32_Process -Filter "Name='postgres.exe'" |
  Where-Object { $_.CommandLine -like "*demo-pgdata*" } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
```

## 已知坑（踩过的，写下来免得再踩）

1. `pg_ctl start` **不能用管道抓输出**：它启动的 postgres 会继承 stdout，父进程读不到 EOF，
   实测卡死 4 分钟。启动类命令必须 `stdout=DEVNULL`。
2. `pgserver.get_server()` 内部 `pg_ctl -w` 只等 10 秒；本机 HDD 在上次非正常关闭后 fsync 恢复可达 30 秒，
   会误报启动失败。演示环境改用自己带 `-t 180` 的 `pg_ctl`。
3. 演示页路由要先 `urlparse(path)` 再匹配，否则 `/?q=...` 会 404。
4. 模型名走 `FKH_LLM_MODEL`（不是 `FKH_OLLAMA_MODEL`），写错会 503。
