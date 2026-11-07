# 设计稿：目录冲突待办的 HTTP 接口（前端第一片的前置）

- 日期：2026-11-07
- 提出方：L2 实施
- 风险归类命中：**对外 API 契约新增**、**权限/身份边界**、**不可逆业务操作（决议）**
- 请只做判断：**不要探索仓库、不要读源码、不要跑测试**。

## 1. 背景

冲突复核目前**只有 CLI**（`fkh.tools.catalog_conflicts list|resolve|export`），
业务前端要把它做成可点的待办页，就必须有 HTTP 接口。现状（已实现、已被 L1 裁决过的领域规则）：

- 组表 `catalog_conflict_groups` 是决议唯一来源；状态 `OPEN / KEEP / DROP_ALL`；
- `resolve_group()` 在**同一事务**里：锁组 → 校验 OPEN → 校验候选归属 → 校验版本可操作 →
  回填条目 → 落决议；不可原地改判（错判走新版本）；
- 决议必须由该租户**活跃 user 主体**执行（`require_operator`，agent/service 不得代签）；
- 版本状态 `REVOKED / DISCARDED / SUPERSEDED` 只读。

本切片只做**读 + 决议**两个端点，不做改判、不做批量决议、不做 UI。

## 2. 拟议接口

### 2.1 `GET /v1/catalog/conflicts`

查询参数：`status`（默认 `OPEN`，可选 `OPEN|KEEP|DROP_ALL`）、`code`（可选，精确匹配）、
`limit`（默认 50，最大 200）、`offset`（默认 0）。

响应（示意）：

```json
{
  "total": 29,
  "limit": 50,
  "offset": 0,
  "items": [
    {
      "group_id": "…",
      "code": "A112440",
      "version_id": "…",
      "status": "OPEN",
      "note": null,
      "resolved_by": null,
      "resolved_at": null,
      "candidates": [
        {"candidate_id": "…", "spec": "400mm 24*24", "raw_price": "57.00",
         "raw_text": "A112440 直形冷凝管 400mm 24*24 57.00",
         "locator": {"page_start": 12, "block_index": 3}}
      ]
    }
  ]
}
```

### 2.2 `POST /v1/catalog/conflicts/{group_id}/resolve`

```json
{"action": "KEEP", "candidate_id": "…", "note": "以主表为准"}
```

- `action=DROP_ALL` 时 `candidate_id` 必须缺省；`KEEP` 时必须给且属于该组；
- **操作者身份只从令牌取**，请求体里没有 actor 字段；
- 成功：200，返回决议后的组（状态、`resolved_by`、`resolved_at`、`note`）；
- 失败：见 §4。

## 3. 可见性与身份（要你确认口径）

1. **权限**：我倾向新增 scope `catalog.resolve`（读用 `catalog.read` 或复用 `knowledge.review`？）。
   理由：决议改的是「事实数据」，与文档审核是不同职责；但新增 scope 会动 `scopes` 契约与令牌签发侧。
2. **可见性**：列表只返回「调用者可见」的组——版本状态在 `OPERABLE_STATUSES` 内、`required_scope` 匹配、
   且 ACL 允许；不可见的组**从 items 剔除，并在过滤后计数**（`total` 只数可见的）。
3. **不可区分**：跨租户 / 不存在 / 不可见，一律返回同一个 404，避免用来探测资源（沿用 `routes_review` 的既有做法，
   那边是 403，这里要不要统一成 404？）。
4. **身份**：调用者的 `principal_type` 必须是 `user` 且 `is_active`；service/agent 令牌返回 403。

## 4. 错误口径（要你定）

| 场景 | 拟议 |
|---|---|
| 组不存在 / 跨租户 / 不可见 | 404 `NOT_FOUND`（与 2.1 一致，不区分） |
| 非 user 主体 / 已停用 | 403 `ACCESS_DENIED` |
| 组已决议（重复提交 / 并发先到者赢） | 409 `VERSION_CONFLICT`（复用现有码）还是新增 `ALREADY_RESOLVED`？ |
| `KEEP` 但候选不属于该组 | 422 `VALIDATION_ERROR` |
| 版本已撤销/弃用 | 409 `VERSION_CONFLICT`（复用） |

我倾向**复用现有错误码**（`NOT_FOUND` / `ACCESS_DENIED` / `VERSION_CONFLICT` / `VALIDATION_ERROR`），
不新增枚举，减少契约面。

## 5. 待判问题（请逐条）

1. scope 怎么定：`catalog.resolve`（新）／复用 `knowledge.review`／只读用 `knowledge.search`？
2. 列表可见性：按 §3.2 剔除不可见组并只数可见的，是否可接受？
3. 404 与 403：跨租户到底统一 404，还是跟 `routes_review` 一样 403？
4. 重复决议：409 + 复用码，还是新增 `ALREADY_RESOLVED`？
5. 响应是否返回 `raw_text`（含客户价格的原文行）？我倾向返回：内网 + 已按 ACL 过滤 + 调用者本身就能查该证据；
   若要收紧，可只给 `spec/raw_price/locator`。
6. 分页与排序：默认 50 / 上限 200，按 `code` 升序（便于运营按货号核对）是否合适？
7. 决议完成后是否需要额外审计事件（`event_envelope` 新增 `catalog.conflict.resolved.v1`）？
   现状只在组行上记 `resolved_by/at/note`；我倾向**不加事件**，避免扩契约。

## 6. 实施侧要补的断言（供裁剪）

1. 读：只返回 OPEN 组；`code` 过滤；分页边界（0 / 超过 total / limit>200 → 422）；
2. 可见性：DRAFT 版本组、ACL 拒绝的组不出现在 items，且 `total` 不含它们；
3. 权限：读/写各自的 scope；缺 scope → 403；
4. 身份：service 令牌决议 → 403，且数据库无写入；
5. 决议：KEEP 成功后条目出现、组变 KEEP；DROP_ALL 后无条目；
6. 重复决议 → 409，且第二次不改变 `resolved_by/at`；
7. 并发：两个请求同时决议同一组，只有一个 200，另一个 409（用锁验证）；
8. 跨租户：A 租户的 group_id 在 B 租户令牌下 → 404；
9. 错误体符合 `error.json`；
10. OpenAPI 与实现一致（契约测试）。

## 7. 边界与预算

- 本切片**不做**：改判、批量决议、占用/认领（claim）、UI；
- 不新增表；不加事件类型（除非你要求）；
- 一次调用回完 §5 的 1–7 + 对 §6 的裁剪；实现后终审另计 1 次。
