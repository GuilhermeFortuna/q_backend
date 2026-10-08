"""Sync MT5 tick history into the research tick store (offline intrabar backtests)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from q_backend.research import TickStore


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="q-sync-ticks",
        description=(
            "Download tick sessions from the MT5 gateway into the research tick store. "
            "Requires Q_MT5_GATEWAY_URL (and token if configured). "
            "Skips days already on disk; never stores the current exchange session."
        ),
    )
    parser.add_argument("--symbol", required=True, help="Symbol to sync, e.g. WDO$N")
    parser.add_argument("--start", required=True, help="First exchange calendar day (YYYY-MM-DD)")
    parser.add_argument("--end", default=None, help="Last day inclusive (default: yesterday in Brasília)")
    parser.add_argument(
        "--root",
        type=Path,
        default=None,
        help="Tick store root (default: Q_RESEARCH_TICK_STORE or data/tick_store)",
    )
    parser.add_argument("--gateway-url", default=None, help="Override Q_MT5_GATEWAY_URL")
    parser.add_argument("--gateway-token", default=None, help="Override Q_MT5_GATEWAY_TOKEN")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    store = TickStore(args.symbol, root=args.root)
    report = store.sync(
        start=args.start,
        end=args.end,
        gateway_url=args.gateway_url,
        gateway_token=args.gateway_token,
    )
    print(report)
    sessions = store.sessions()
    if sessions:
        print(
            f"\n{len(sessions)} session(s) on disk; earliest {sessions[0].isoformat()}, latest {sessions[-1].isoformat()}"
        )
    else:
        print("\nNo sessions stored yet for this symbol.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
