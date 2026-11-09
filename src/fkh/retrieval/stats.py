"""检索统计表：精确 N/avg_len/df 的重建与读取。

统计表只保存**主体无关**的 `(tenant, scope, internal, acl_policy_id)` 聚合；
请求侧仍按当前主体对 ACL 分组求值，再累计可见分组的 N/df。
统计表未 READY、built_revision != source_revision、或没有该 scope 数据时，
调用方必须回退 `lexical._STATS_SQL` 的 live 路径。

重建要求调用方在 **REPEATABLE READ** 事务中执行（CLI 已设置）；同一 tenant 通过
状态行 `FOR UPDATE` 串行化。触发器在数据/版本/ACL 变化时递增 source_revision，
因此重建提交后若又有写提交，built_revision 会落后，读侧自动回退 live。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import psycopg
from psycopg import sql

from fkh.retrieval.visibility import TOKENIZER_ID, VISIBILITY_PREDICATE

SCOPES = ("internal_product", "internal_training", "customer_public")
_TERM_BATCH = 1000


@dataclass(frozen=True, slots=True)
class StatsGroup:
    acl_policy_id: UUID
    policy_json: Any
    docs: int
    total_len: int
    df: dict[str, int]


@dataclass(frozen=True, slots=True)
class StatsSnapshot:
    tenant_id: UUID
    generation: int
    built_revision: int
    source_revision: int
    groups: tuple[StatsGroup, ...]

    @property
    def n(self) -> int:
        return sum(group.docs for group in self.groups)

    @property
    def avg_len(self) -> float:
        total = sum(group.total_len for group in self.groups)
        return (total / self.n) if self.n else 0.0


def _base_sql() -> str:
    return f"""
        SELECT c.acl_policy_id, count(*), coalesce(sum(length(c.chunk_text)), 0)
          FROM knowledge_chunks c
          JOIN knowledge_versions v
            ON v.tenant_id = c.tenant_id AND v.id = c.version_id
          LEFT JOIN acl_policies p
            ON p.tenant_id = c.tenant_id AND p.id = c.acl_policy_id
         WHERE c.tenant_id = %(tenant)s
           AND c.chunk_kind = 'child'
           AND c.lexeme_tokenizer_id = %(lexeme_tokenizer)s
           AND (v.status <> 'PUBLISHED_PUBLIC' OR c.index_generation <= COALESCE(
                 (SELECT generation FROM index_visibility iv WHERE iv.tenant_id = %(tenant)s), 0))
           {VISIBILITY_PREDICATE}
         GROUP BY c.acl_policy_id
    """


def _ts_stat_sql(tenant: UUID, acl: UUID, *, scope: str, internal: bool) -> str:
    """构造 ts_stat 的内层 SELECT（ts_stat 只接受文本 SQL）。"""
    predicate = VISIBILITY_PREDICATE % {
        "scope": sql.Literal(scope).as_string(None),
        "internal": "true" if internal else "false",
    }
    return f"""
        SELECT c.lexeme
          FROM knowledge_chunks c
          JOIN knowledge_versions v
            ON v.tenant_id = c.tenant_id AND v.id = c.version_id
         WHERE c.tenant_id = {sql.Literal(str(tenant)).as_string(None)}
           AND c.acl_policy_id = {sql.Literal(str(acl)).as_string(None)}
           AND c.chunk_kind = 'child'
           AND c.lexeme_tokenizer_id = {sql.Literal(TOKENIZER_ID).as_string(None)}
           AND (v.status <> 'PUBLISHED_PUBLIC' OR c.index_generation <= COALESCE(
                 (SELECT generation FROM index_visibility iv
                   WHERE iv.tenant_id = {sql.Literal(str(tenant)).as_string(None)}), 0))
           {predicate}
    """


def _upsert_state(conn: psycopg.Connection[Any], tenant: UUID) -> tuple[int, int]:
    conn.execute(
        "INSERT INTO retrieval_stats_state (tenant_id, source_revision, status)"
        " VALUES (%s, 0, 'EMPTY') ON CONFLICT (tenant_id) DO NOTHING",
        (tenant,),
    )
    row = conn.execute(
        "SELECT source_revision, generation FROM retrieval_stats_state"
        " WHERE tenant_id = %s FOR UPDATE",
        (tenant,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"无法锁定统计状态：{tenant}")
    return int(row[0]), int(row[1])


def refresh_tenant(conn: psycopg.Connection[Any], tenant: UUID) -> dict[str, Any]:
    """重建一个 tenant 的统计；调用方负责事务/隔离级别。

    返回摘要（不含原文）。失败会抛出，由调用方 rollback；旧 generation 保持可用。
    """
    isolation = conn.execute("SHOW transaction_isolation").fetchone()
    if isolation is None or str(isolation[0]).lower() != "repeatable read":
        raise RuntimeError("refresh_tenant 必须在 REPEATABLE READ 事务中执行")
    source_revision, old_generation = _upsert_state(conn, tenant)
    generation = old_generation + 1
    groups = 0
    terms = 0

    for internal in (True, False):
        for scope in SCOPES:
            rows = conn.execute(
                _base_sql(),
                {
                    "tenant": tenant,
                    "scope": scope,
                    "internal": internal,
                    "lexeme_tokenizer": TOKENIZER_ID,
                },
            ).fetchall()
            for acl_raw, docs_raw, total_raw in rows:
                acl = UUID(str(acl_raw))
                docs = int(docs_raw)
                total = int(total_raw)
                conn.execute(
                    """INSERT INTO retrieval_corpus_stats
                         (tenant_id, generation, scope, internal, acl_policy_id, docs, total_len)
                       VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                    (tenant, generation, scope, internal, acl, docs, total),
                )
                groups += 1
                term_rows = conn.execute(
                    "SELECT word, ndoc FROM ts_stat(%s)",
                    (_ts_stat_sql(tenant, acl, scope=scope, internal=internal),),
                ).fetchall()
                batch: list[tuple[Any, ...]] = []
                for term_raw, ndoc_raw in term_rows:
                    term = str(term_raw)
                    df = min(int(ndoc_raw), docs)
                    batch.append((tenant, generation, scope, internal, acl, term, df, docs))
                    if len(batch) >= _TERM_BATCH:
                        with conn.cursor() as cursor:
                            cursor.executemany(
                                """INSERT INTO retrieval_term_stats
                                     (tenant_id, generation, scope, internal, acl_policy_id,
                                      term, df, docs)
                                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                                batch,
                            )
                        terms += len(batch)
                        batch.clear()
                if batch:
                    with conn.cursor() as cursor:
                        cursor.executemany(
                            """INSERT INTO retrieval_term_stats
                                 (tenant_id, generation, scope, internal, acl_policy_id,
                                  term, df, docs)
                               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                            batch,
                        )
                    terms += len(batch)

    status = "READY" if groups else "EMPTY"
    conn.execute(
        """UPDATE retrieval_stats_state
              SET built_revision = %s, generation = %s, status = %s,
                  refreshed_at = now(), last_error = NULL
            WHERE tenant_id = %s""",
        (source_revision, generation, status, tenant),
    )
    # 旧 generation 只在状态发布后删除；同一事务提交对读侧原子可见。
    conn.execute(
        "DELETE FROM retrieval_term_stats WHERE tenant_id = %s AND generation < %s",
        (tenant, generation),
    )
    conn.execute(
        "DELETE FROM retrieval_corpus_stats WHERE tenant_id = %s AND generation < %s",
        (tenant, generation),
    )
    return {
        "tenant_id": str(tenant),
        "generation": generation,
        "source_revision": source_revision,
        "status": status,
        "groups": groups,
        "terms": terms,
    }


def load_snapshot(
    conn: psycopg.Connection[Any],
    tenant: UUID,
    *,
    scope: str,
    internal: bool,
    terms: list[str],
) -> StatsSnapshot | None:
    """读取有效统计；未构建/失效/无该 scope 时返回 None（调用方回退 live）。"""
    state = conn.execute(
        """SELECT status, built_revision, source_revision, generation
             FROM retrieval_stats_state WHERE tenant_id = %s""",
        (tenant,),
    ).fetchone()
    if state is None:
        return None
    status, built, source, generation = str(state[0]), state[1], state[2], state[3]
    if status not in {"READY", "EMPTY"} or built is None or int(built) != int(source):
        return None
    if status == "EMPTY":
        return StatsSnapshot(tenant, int(generation), int(built), int(source), ())
    # ts_stat 的 word 是 text search 规范化后的 lexeme（Latin 会转小写）；
    # 查询 term 来自 segment_cjk，大小写未变。这里统一按小写查找，返回时由调用方映射回原词。
    normalized_terms = sorted({term.lower() for term in terms})
    rows = conn.execute(
        """SELECT s.acl_policy_id, p.policy_json, s.docs, s.total_len, t.term, t.df
             FROM retrieval_corpus_stats s
             JOIN acl_policies p
               ON p.tenant_id = s.tenant_id AND p.id = s.acl_policy_id
             LEFT JOIN retrieval_term_stats t
               ON t.tenant_id = s.tenant_id AND t.generation = s.generation
              AND t.scope = s.scope AND t.internal = s.internal
              AND t.acl_policy_id = s.acl_policy_id
              AND t.term = ANY(%s)
            WHERE s.tenant_id = %s AND s.generation = %s
              AND s.scope = %s AND s.internal = %s
            ORDER BY s.acl_policy_id""",
        (normalized_terms, tenant, int(generation), scope, internal),
    ).fetchall()
    if not rows:
        return None
    grouped: dict[UUID, dict[str, Any]] = {}
    for acl_raw, policy, docs, total, term, df in rows:
        acl = UUID(str(acl_raw))
        entry = grouped.setdefault(
            acl, {"policy": policy, "docs": int(docs), "total": int(total), "df": {}}
        )
        if term is not None:
            entry["df"][str(term)] = int(df)
    groups = tuple(
        StatsGroup(
            acl_policy_id=acl,
            policy_json=entry["policy"],
            docs=entry["docs"],
            total_len=entry["total"],
            df=entry["df"],
        )
        for acl, entry in grouped.items()
    )
    return StatsSnapshot(tenant, int(generation), int(built), int(source), groups)


def dirty_tenants(conn: psycopg.Connection[Any]) -> list[UUID]:
    """需要重建的 tenant：未构建、revision 落后、或上次刷新失败。"""
    rows = conn.execute(
        """SELECT tenant_id FROM retrieval_stats_state
            WHERE built_revision IS NULL
               OR built_revision <> source_revision
               OR status = 'FAILED'
            ORDER BY tenant_id"""
    ).fetchall()
    return [UUID(str(row[0])) for row in rows]


def mark_failed(url: str, tenant: UUID, error: str) -> None:
    """在独立事务标记 FAILED；失败细节只留在运维状态里，不进检索响应。

    条件更新避免较旧的失败覆盖较新的成功：若当前 built=source 且 READY/EMPTY，
    说明已有新一轮刷新成功，这次失败只上报、不污染状态。
    """
    detail = error[:1000]
    with psycopg.connect(url, autocommit=False) as conn:
        conn.execute(
            """UPDATE retrieval_stats_state
                  SET status = 'FAILED', last_error = %s
                WHERE tenant_id = %s
                  AND NOT (
                      built_revision IS NOT NULL
                      AND built_revision = source_revision
                      AND status IN ('READY', 'EMPTY')
                  )""",
            (detail, tenant),
        )
        conn.commit()


def _send_alert(webhook: str, payload: dict[str, Any]) -> None:
    """把失败事件 POST 到运维 webhook；送达失败只记录，不改变刷新退出码。"""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        webhook,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:  # noqa: S310 - 运维配置的固定 URL
            response.read(1024)
    except Exception as exc:  # noqa: BLE001 - 告警失败不能吞掉刷新失败
        print(
            json.dumps(
                {
                    "event": "retrieval_stats_alert_delivery_failed",
                    "error": f"{type(exc).__name__}: {exc}",
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )


def refresh_dirty_once(
    url: str,
    *,
    limit: int | None = None,
    alert_webhook: str | None = None,
    tenants: list[UUID] | None = None,
) -> dict[str, Any]:
    """刷新所有脏 tenant 一次；返回成功/失败计数，失败不会中断其他 tenant。

    `tenants` 只供受控调用/测试指定子集；CLI 默认查全部。
    """
    if tenants is None:
        with psycopg.connect(url, autocommit=False) as conn:
            tenants = dirty_tenants(conn)
            conn.rollback()
    else:
        tenants = list(tenants)
    if limit is not None:
        tenants = tenants[:limit]
    refreshed = 0
    failed: list[dict[str, str]] = []
    for tenant in tenants:
        try:
            summary = _refresh_one(url, tenant)
            refreshed += 1
            print(json.dumps({"event": "retrieval_stats_refreshed", **summary}, ensure_ascii=False))
        except Exception as exc:  # noqa: BLE001 - 单个 tenant 失败不能中断巡检
            failed.append({"tenant_id": str(tenant), "error": f"{type(exc).__name__}: {exc}"})
            try:
                mark_failed(url, tenant, f"{type(exc).__name__}: {exc}")
            except Exception as mark_exc:  # noqa: BLE001 - 标记失败也要继续巡检
                print(
                    json.dumps(
                        {
                            "event": "retrieval_stats_mark_failed_error",
                            "tenant_id": str(tenant),
                            "error": f"{type(mark_exc).__name__}: {mark_exc}",
                        },
                        ensure_ascii=False,
                    ),
                    file=sys.stderr,
                )
            print(
                json.dumps(
                    {"event": "retrieval_stats_refresh_failed", **failed[-1]},
                    ensure_ascii=False,
                ),
                file=sys.stderr,
            )
    result = {"dirty": len(tenants), "refreshed": refreshed, "failed": failed}
    if failed and alert_webhook:
        _send_alert(
            alert_webhook,
            {"event": "retrieval_stats_alert", **result},
        )
    return result


def _refresh_one(url: str, tenant: UUID, *, attempts: int = 3) -> dict[str, Any]:
    """一次 tenant 重建；REPEATABLE READ 下并发状态变更可能 40001，整事务重试。"""
    for attempt in range(attempts):
        try:
            with psycopg.connect(url, autocommit=False) as conn:
                conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
                summary = refresh_tenant(conn, tenant)
                conn.commit()
                return summary
        except psycopg.errors.SerializationFailure:
            if attempt + 1 >= attempts:
                raise
            time.sleep(0.1 * (attempt + 1))
    raise AssertionError("unreachable")


def _watch(
    url: str,
    *,
    interval: float,
    max_iterations: int | None,
    alert_webhook: str | None = None,
) -> int:
    iteration = 0
    while max_iterations is None or iteration < max_iterations:
        iteration += 1
        result = refresh_dirty_once(url, alert_webhook=alert_webhook)
        if result["failed"]:
            return 1
        if max_iterations is not None and iteration >= max_iterations:
            break
        time.sleep(max(0.2, interval))
    return 0


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="重建检索统计表")
    parser.add_argument("--url", required=True, help="数据库 URL")
    parser.add_argument("--tenant", help="只重建一个 tenant UUID")
    parser.add_argument("--all", action="store_true", help="重建所有 tenant")
    parser.add_argument("--dirty", action="store_true", help="只重建未构建/失效/FAILED 的 tenant")
    parser.add_argument("--watch", action="store_true", help="持续巡检并重建脏 tenant")
    parser.add_argument("--interval", type=float, default=5.0, help="巡检间隔秒数")
    parser.add_argument("--max-iterations", type=int, default=None, help="测试/一次性巡检轮数")
    parser.add_argument(
        "--alert-webhook",
        default=os.environ.get("FKH_STATS_ALERT_WEBHOOK"),
        help="刷新失败时 POST JSON 的运维 webhook",
    )
    args = parser.parse_args(argv)
    modes = [bool(args.tenant), args.all, args.dirty, args.watch]
    if sum(modes) != 1:
        raise SystemExit("--tenant / --all / --dirty / --watch 必须四选一")

    if args.watch:
        return _watch(
            args.url,
            interval=args.interval,
            max_iterations=args.max_iterations,
            alert_webhook=args.alert_webhook,
        )
    if args.dirty:
        result = refresh_dirty_once(args.url, alert_webhook=args.alert_webhook)
        return 0 if not result["failed"] else 1
    if args.all:
        with psycopg.connect(args.url, autocommit=False) as conn:
            tenants = [
                UUID(str(row[0]))
                for row in conn.execute("SELECT id FROM tenants ORDER BY id").fetchall()
            ]
            conn.rollback()
    else:
        tenants = [UUID(args.tenant)]
    for tenant in tenants:
        summary = _refresh_one(args.url, tenant)
        print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
