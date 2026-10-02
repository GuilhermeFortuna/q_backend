"""CLI for the standalone live market-data publisher."""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading

import redis

from q_backend.market_data.clients.remote import RemoteMt5Client
from q_backend.observability.systemd import EX_CONFIG, notify_ready
from q_backend.storage.settings import get_settings
from q_backend.streaming.market.publisher import MarketDataPublisher, MarketPublisherConfig
from q_backend.streaming.market.trade_history import TradeService, cache_from_settings
from q_backend.streaming.market.trades import TradeSessionCoordinator, TradeStreamSink
from q_backend.streaming.publisher import EphemeralPublisher
from q_backend.streaming.redis_binary import get_binary_redis

logger = logging.getLogger("q_backend.cli.q_market_publisher")


def _csv(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="q-market-publisher")
    parser.add_argument("--symbols")
    parser.add_argument("--timeframes")
    return parser


def build_trade_service(
    client: redis.Redis, remote: RemoteMt5Client, symbols: tuple[str, ...], settings
) -> TradeService:
    """One session coordinator per symbol, all feeding the shared trades topics."""
    sink = TradeStreamSink(
        EphemeralPublisher(client, "trades", producer_id="market-publisher"),
        EphemeralPublisher(client, "trades.status", producer_id="market-publisher"),
    )
    coordinators = {
        symbol: TradeSessionCoordinator(
            remote,
            symbol,
            sink,
            settle_ms=settings.trade_group_settle_ms,
            poll_interval_s=settings.trade_poll_interval_s,
        )
        for symbol in symbols
    }
    return TradeService(
        client,
        cache_from_settings(settings),
        coordinators,
        max_backfills=settings.trade_cache_max_backfills,
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = get_settings()
    symbols = _csv(args.symbols if args.symbols is not None else settings.stream_symbols)
    if not symbols:
        sys.stderr.write("no symbols configured\n")
        return EX_CONFIG
    timeframes = _csv(args.timeframes if args.timeframes is not None else settings.stream_bar_timeframes)
    logging.basicConfig(level=logging.INFO)
    client = get_binary_redis()

    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())

    # Wait for Redis connection with capped backoff before notifying readiness
    backoff = 0.5
    max_backoff = 30.0
    while not stop.is_set():
        try:
            client.ping()
            notify_ready()
            break
        except redis.RedisError as exc:
            logger.warning("Redis not ready: %s. Retrying in %.2fs", exc, backoff)
            stop.wait(backoff)
            backoff = min(backoff * 2, max_backoff)

    if stop.is_set():
        return 0

    remote = RemoteMt5Client()
    publisher = MarketDataPublisher(
        remote,
        {
            "quotes": EphemeralPublisher(client, "quotes", producer_id="market-publisher"),
            "bars.forming": EphemeralPublisher(client, "bars.forming", producer_id="market-publisher"),
            "bars.completed": EphemeralPublisher(client, "bars.completed", producer_id="market-publisher"),
        },
        MarketPublisherConfig(
            symbols=symbols,
            timeframes=timeframes,
            tick_poll_interval_s=settings.stream_tick_poll_interval_s,
            bar_poll_interval_s=settings.stream_bar_poll_interval_s,
        ),
        trades=build_trade_service(client, remote, symbols, settings),
    )
    publisher.run_forever(stop)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
