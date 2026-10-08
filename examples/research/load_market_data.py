#!/usr/bin/env python3
"""Load OHLCV bars through q_backend.research (see docs/research-library.md).

Example::

    uv run python examples/research/load_market_data.py --symbol WIN$N --start 2026-09-01
"""

from __future__ import annotations

import argparse
import os

from q_backend.research import load_bars


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Load fresh MT5 OHLCV bars via the Q gateway.")
    parser.add_argument(
        "--symbol",
        required=True,
        help="MT5 symbol (e.g. WIN$N for unadjusted B3 continuous futures)",
    )
    parser.add_argument("--timeframe", default="M5")
    parser.add_argument("--start", required=True, help="Inclusive bar-open bound (ISO or YYYY-MM-DD)")
    parser.add_argument("--end", default=None, help="Inclusive bar-open bound; default is now")
    parser.add_argument("--gateway-url", default=os.getenv("Q_MT5_GATEWAY_URL"))
    parser.add_argument("--gateway-token", default=os.getenv("Q_MT5_GATEWAY_TOKEN"))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    frame = load_bars(
        args.symbol,
        timeframe=args.timeframe,
        start=args.start,
        end=args.end,
        gateway_url=args.gateway_url,
        gateway_token=args.gateway_token,
    )
    print(frame.head())
    print(f"rows={len(frame)} cols={list(frame.columns)}")
    meta = frame.attrs.get("q_research", {})
    if meta:
        print(f"source={meta.get('source')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
