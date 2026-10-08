#!/usr/bin/env python3
"""Sync research tick history from the MT5 gateway into a local tick store."""

from __future__ import annotations

import argparse
from pathlib import Path

from q_backend.research import TickStore


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Sync MT5 tick history into the research tick store.")
    parser.add_argument("--symbol", required=True, help="Symbol to sync, e.g. WDO$N")
    parser.add_argument("--start", required=True, help="First exchange calendar day (YYYY-MM-DD)")
    parser.add_argument("--end", default=None, help="Last day inclusive (default: yesterday in Brasília)")
    parser.add_argument(
        "--root", default=None, help="Tick store root (default: Q_RESEARCH_TICK_STORE or data/tick_store)"
    )
    parser.add_argument("--gateway-url", default=None, help="Override Q_MT5_GATEWAY_URL")
    parser.add_argument("--gateway-token", default=None, help="Override Q_MT5_GATEWAY_TOKEN")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = Path(args.root) if args.root else None
    store = TickStore(args.symbol, root=root)
    report = store.sync(
        start=args.start,
        end=args.end,
        gateway_url=args.gateway_url,
        gateway_token=args.gateway_token,
    )
    print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
