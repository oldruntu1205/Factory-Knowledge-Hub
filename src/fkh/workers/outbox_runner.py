"""outbox relay 的命令行入口。

- 未配置传输层时**拒绝启动**（fail-closed）：宁可让人看见「投递没跑」，
  也不要让事件在 `published_at` 上显示为已投递而实际从未发出。
- 连接串必须指向以 `fkh_relay`（BYPASSRLS）登录的数据库：
  用业务角色跑会静默扫到零行，表现为「relay 在跑但什么也不发」。
"""

from __future__ import annotations

import argparse
import logging
import sys
import time

import psycopg

from fkh.config import get_settings
from fkh.workers.outbox_relay import (
    OutboxRelay,
    TransportNotConfigured,
    build_transport,
    dead_letter_count,
    relay_config_from_settings,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Factory Knowledge Hub · outbox 投递器")
    parser.add_argument("--once", action="store_true", help="只跑一轮后退出")
    parser.add_argument("--interval", type=float, default=5.0, help="轮询间隔（秒）")
    parser.add_argument("--limit", type=int, default=None, help="单轮最多领取条数")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    log = logging.getLogger("fkh.outbox.relay")

    settings = get_settings()
    if not settings.database_url:
        raise SystemExit("未配置 FKH_DATABASE_URL，relay 无法启动")

    try:
        transport = build_transport(settings)
    except TransportNotConfigured as exc:
        raise SystemExit(f"relay 拒绝启动: {exc}") from exc

    config = relay_config_from_settings(settings)
    log.info(
        "relay 启动: batch=%s lease=%ss max_attempts=%s producer=%s",
        config.batch_size,
        config.lease_seconds,
        config.max_attempts,
        config.producer,
    )

    with psycopg.connect(settings.database_url, autocommit=False) as conn:
        relay = OutboxRelay(conn, transport, config)
        while True:
            result = relay.run_once(args.limit)
            if result.claimed or result.expired_recovered:
                log.info(
                    "本轮: claimed=%s delivered=%s failed=%s dead=%s lease_lost=%s recovered=%s",
                    result.claimed,
                    result.delivered,
                    result.failed,
                    result.dead_lettered,
                    result.lease_lost,
                    result.expired_recovered,
                )
            pending_dead = dead_letter_count(conn)
            if pending_dead:
                # 死信必须可见：它阻塞同聚合的后续事件，没人处理就会静默停摆
                log.error("存在 %s 条死信事件，需要人工重放或显式废弃", pending_dead)

            if args.once:
                return 0
            time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
