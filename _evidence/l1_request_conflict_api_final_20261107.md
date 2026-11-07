# L1 终审：冲突待办 HTTP 接口（`e2adc33`）

- 日期：2026-11-07
- 对应裁决：`_evidence/l1_reply_conflict_api_20261107.md`（有条件通过）
- 请只做判断：**不要探索仓库、不要读源码、不要跑测试**。

## 1. 逐条落实

| 裁决 | 实现 |
|---|---|
| 读 `catalog.read`、决议再加 `catalog.resolve`；不复用 knowledge.* | 是；两 scope 由令牌签发侧登记，既有令牌不自动获得 |
| POST 身份服务端复核（活跃 user），不信签发态 | 是：`require_operator`（该租户 / `principal_type=user` / `is_active`） |
| 过滤先于计数与分页 | 是：SQL 过滤租户/状态/scope → 应用层 ACL 过滤 → `total` 计数 → 切片 |
| 不可见 = 版本不可操作 / scope 不符 / ACL 拒绝 | 是；ACL 挂在 **候选 chunk** 上，任一条被拒即整组隐藏 |
| 资源级拒绝 404、资格不足 403 | 是；新增 `NOT_FOUND`（`error.json` + `errors.py`），错误体统一 |
| 访问获准后版本不可操作 → 409 | 是（先判可见性，再判 `OPERABLE_STATUSES`） |
| 重复决议 409 复用 `VERSION_CONFLICT`，第二次不改字段 | 是；有断言比对 `resolved_at/resolved_by/note` 前后一致 |
| 响应不含 `raw_text`；KEEP 返回所选候选 | 是；`selected_candidate_id` 在 DROP_ALL/OPEN 为 null |
| 排序 `code ASC, group_id ASC`；limit 1–200；offset ≥0 | 是；越界返回空列表且 `total` 保持 |
| 不新增审计事件；组行承担业务决议记录 | 是；未动 event 契约，文档注明"非独立防篡改审计日志" |
| KEEP 缺候选 / 候选越组 / DROP_ALL 带候选人（含 null）→ 422 | 是；`extra="forbid"` + `model_fields_set` 判定 |

## 2. 新增/改动

- `src/fkh/api/routes_catalog_conflicts.py`（新）
- `errors.py` + `error.json`：新增 `NOT_FOUND`（404）
- `contracts/schema/catalog_conflict_list.json`、`catalog_conflict_group.json`
- `openapi.yaml`：两条路径 + 组件引用
- `tests/e2e/test_conflict_api.py`（12 项）

## 3. 门禁

```
uv run pytest -q                → 524 passed
uv run pytest -q -m requires_pg → 318 passed, 0 skip
ruff / mypy                     → 全绿
```

## 4. 测试覆盖（对应你裁剪后的六类）

1. 查询与分页：默认 OPEN、显式 KEEP/DROP_ALL、code 精确、越界、非法 limit/status；
2. 可见性：不可操作版本、ACL 拒绝、跨租户；断言 items 与 total，并验证隐藏组不占分页位；
3. 权限与身份：缺 read / 缺 resolve / service 决议 403 且无写入；资源级 404；
   具备读取授权但版本 SUPERSEDED → 409；
4. 决议与校验：KEEP、DROP_ALL 成功；KEEP 缺候选、候选越组、DROP_ALL 带 candidate_id（显式 null）→ 422；
5. 重复与并发：真实事务锁下两请求恰好一个 200、一个 409；重复请求不改写字段；
6. 契约：列表响应按 JSON Schema 校验；错误体按 `error.json` 校验；OpenAPI 路径集合一致。

## 5. 已知限制（照实写）

1. 列表是"全量过滤后再切片"，租户内组数很大时内存 O(n)；本片不承诺浏览期间快照一致性；
2. 组行只记业务决议（状态/人/时间/备注/所选候选），**不是防篡改审计日志**；
3. 不做改判、批量决议、认领；
4. ACL 以"任一候选被拒即整组隐藏"处理——复核人员不会看到残缺对照表，但也会因此看不到该组。

请裁决：**PASS** / **CHANGES_REQUESTED（逐条）**。
