"""Standalone transactional outbox relay CLI."""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading

import sqlalchemy as sa
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from q_backend.observability.sentry import init_sentry
from q_backend.storage.db.engine import get_engine
from q_backend.storage.settings import get_settings
from q_backend.streaming.redis_binary import get_binary_redis
from q_backend.streaming.relay import OutboxRelay, RelayConfig

logger = logging.getLogger("q_backend.cli.q_outbox_relay")

# 64-bit constant integer for session-level advisory lock (ASCII for "Q_OUTBOX")
OUTBOX_RELAY_ADVISORY_LOCK_KEY = 5863503259837026136


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="q-outbox-relay",
        description="Transactional outbox to Redis Streams relay service",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=0.2,
        help="Poll interval in seconds (default: 0.2)",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=500,
        help="Batch size per topic per pass (default: 500)",
    )
    parser.add_argument(
        "--prune-interval",
        type=float,
        default=3600.0,
        help="Pruning interval in seconds (default: 3600.0)",
    )
    parser.add_argument(
        "--max-backoff",
        type=float,
        default=30.0,
        help="Maximum backoff on outage in seconds (default: 30.0)",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        help="Log level (default: INFO)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    settings = get_settings()
    init_sentry(settings, component="cli")

    engine = get_engine()
    lock_conn = engine.connect()

    # Session-level Postgres advisory lock
    acquired = lock_conn.execute(
        text("SELECT pg_try_advisory_lock(:key)"),
        {"key": OUTBOX_RELAY_ADVISORY_LOCK_KEY},
    ).scalar()

    if not acquired:
        sys.stderr.write("Another outbox relay instance is already running.\n")
        logger.error("Another outbox relay instance is already running; refusing to start.")
        lock_conn.close()
        return 1

    logger.info("Acquired outbox relay advisory lock. Starting relay service...")

    stop_event = threading.Event()

    def handle_signal(signum: int, _frame: object) -> None:
        logger.info("Received termination signal %s. Initiating graceful shutdown...", signum)
        stop_event.set()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    client = get_binary_redis()
    config = RelayConfig(
        poll_interval_s=args.poll_interval,
        batch_size=args.batch_size,
        prune_interval_s=args.prune_interval,
        max_backoff_s=args.max_backoff,
    )

    relay = OutboxRelay(session_factory, client, config)

    try:
        relay.run_forever(stop_event)
    finally:
        logger.info("Releasing outbox relay advisory lock...")
        try:
            lock_conn.execute(
                text("SELECT pg_advisory_unlock(:key)"),
                {"key": OUTBOX_RELAY_ADVISORY_LOCK_KEY},
            )
        except sa.exc.SQLAlchemyError as exc:
            logger.warning("Error unlocking advisory lock: %s", exc)
        finally:
            lock_conn.close()

    logger.info("Outbox relay shutdown cleanly.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
