#!/usr/bin/env python3
"""Load OHLCV bars through q_backend.research (see docs/research-library.md)."""

from __future__ import annotations

import argparse
import os
from q_backend.research import Research


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Load catalog or gateway OHLCV bars.")
    parser.add_argument("--source", choices=("local", "remote", "auto"), default="local")
    parser.add_argument("--database-url", default=os.getenv("Q_DATABASE_URL"))
    parser.add_argument("--market-data-root", default=os.getenv("Q_MARKET_DATA_ROOT"))
    parser.add_argument("--gateway-url", default=os.getenv("Q_MT5_GATEWAY_URL"))
    parser.add_argument("--gateway-token", default=os.getenv("Q_MT5_GATEWAY_TOKEN"))
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--timeframe", default="M5")
    parser.add_argument("--start", required=True, help="Inclusive bar-open bound (ISO or YYYY-MM-DD)")
    parser.add_argument("--end", required=True, help="Inclusive bar-open bound (ISO or YYYY-MM-DD)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    with Research(
        source=args.source,
        database_url=args.database_url,
        market_data_root=args.market_data_root,
        gateway_url=args.gateway_url,
        gateway_token=args.gateway_token,
    ) as research:
        frame = research.bars(
            args.symbol,
            timeframe=args.timeframe,
            start=args.start,
            end=args.end,
        )
    print(frame.head())
    print(f"rows={len(frame)} cols={list(frame.columns)}")
    meta = frame.attrs.get("q_research", {})
    if meta:
        print(f"source={meta.get('source')} dataset_id={meta.get('dataset_id')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
