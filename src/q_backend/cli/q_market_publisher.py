"""CLI for the standalone live market-data publisher."""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading

from q_backend.market_data.clients.remote import RemoteMt5Client
from q_backend.storage.settings import get_settings
from q_backend.streaming.market.publisher import MarketDataPublisher, MarketPublisherConfig
from q_backend.streaming.publisher import EphemeralPublisher
from q_backend.streaming.redis_binary import get_binary_redis


def _csv(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="q-market-publisher")
    parser.add_argument("--symbols")
    parser.add_argument("--timeframes")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = get_settings()
    symbols = _csv(args.symbols if args.symbols is not None else settings.stream_symbols)
    if not symbols:
        sys.stderr.write("no symbols configured\n")
        return 1
    timeframes = _csv(args.timeframes if args.timeframes is not None else settings.stream_bar_timeframes)
    logging.basicConfig(level=logging.INFO)
    client = get_binary_redis()
    publisher = MarketDataPublisher(
        RemoteMt5Client(),
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
    )
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    publisher.run_forever(stop)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
