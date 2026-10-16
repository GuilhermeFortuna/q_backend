"""Profile a synced week and publish prior-session value-area levels as a study."""

from __future__ import annotations

import argparse

from q_backend.research import indicators, load_bars
from q_backend.research.tick_store import TickStore


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default="WIN$N")
    parser.add_argument("--start", default="2026-10-01")
    parser.add_argument("--end", default="2026-10-08")
    parser.add_argument("--tick-size", type=float, default=None)
    args = parser.parse_args()

    ticks = TickStore(args.symbol)
    bars = load_bars(args.symbol, "M5", start=args.start, end=args.end)
    tick_size = args.tick_size
    if tick_size is None:
        meta = bars.attrs.get("q_research", {})
        tick_size = float(meta.get("tick_size", 0.5))

    profile = indicators.volume_at_price(
        ticks,
        start=args.start,
        end=args.end,
        tick_size=tick_size,
        by="session",
    )
    print(profile.segments[["total", "poc", "vah", "val"]])

    levels = indicators.value_area_levels(
        bars,
        ticks=ticks,
        timeframe="M5",
        mode="prior_session",
        tick_size=tick_size,
    )
    print(levels.tail())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
