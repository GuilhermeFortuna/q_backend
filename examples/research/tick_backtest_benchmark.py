#!/usr/bin/env python3
"""Time a trailing-stop backtest over a synthetic TickStore, with and without parallel sessions.

The example writes a deterministic multi-session store into a temporary directory, so it runs
offline. Each session is a day of M10 bars with eight trade prints per bar. The entry rule and
the trailing stop are the same in both runs; only the way the stop moves differs:

- ``amend``: the stop moves to each closed bar's low with ``TradeOrder.amend``, confirmed from ticks;
- ``priced``: the stop is a per-bar priced close calculated from the prior ten bars' lows.

Each strategy runs once with ``workers=1`` and once with ``--workers``. Parallel sessions apply
to day-trade runs only, so the benchmark runs in day-trade mode. See docs/research-library.md.
"""

from __future__ import annotations

import argparse
import tempfile
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from q_backend.research import ResearchStrategy, TickStore, TradeOrder, backtest
from q_backend.research.tick_store import _slug_symbol, _write_day_atomic

SYMBOL = "WDO$N"
FIRST_DAY = date(2026, 1, 5)
BARS_PER_SESSION = 42  # M10 bars from 09:00 to 16:00
TICKS_PER_BAR = 8
ENTRY_EVERY = 3
EMA_SPAN = 20
ATR_BARS = 14
STOP_POINTS = 4.0
TRAIL_BARS = 10


def write_sessions(root: Path, sessions: int, seed: int = 7) -> TickStore:
    """Write ``sessions`` days of trade prints under ``root`` and return the store."""
    rng = np.random.default_rng(seed)
    directory = root / _slug_symbol(SYMBOL)
    directory.mkdir(parents=True, exist_ok=True)
    price = 100.0
    for day_offset in range(sessions):
        day = FIRST_DAY + timedelta(days=day_offset)
        midnight = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
        times, prices = [], []
        for bar in range(BARS_PER_SESSION):
            bar_start = midnight + timedelta(hours=9, minutes=10 * bar)
            for tick in range(TICKS_PER_BAR):
                moment = bar_start + timedelta(minutes=tick)
                # MT5 stores broker wall-clock time as the UTC components of the epoch.
                times.append(int(moment.timestamp() * 1000))
                # Mean-reverting around 100, so prices cross the EMA in both directions.
                price = round((100.0 + 0.97 * (price - 100.0) + rng.normal(0.0, 0.6)) * 2) / 2
                prices.append(price)
        values = np.asarray(prices, dtype=np.float64)
        arrays = {
            "time_msc": np.asarray(times, dtype=np.int64),
            "bid": values,
            "ask": values,
            "last": values,
            "volume": np.ones(len(values), dtype=np.float64),
            "flags": np.full(len(values), 32, dtype=np.int32),
        }
        _write_day_atomic(directory / f"{day.isoformat()}.parquet", arrays)
    return TickStore(SYMBOL, root=root)


class _Entries(ResearchStrategy):
    """Buys every few bars above an EMA, with a stop ``STOP_POINTS`` below the close."""

    def compute_indicators(self, frame: pd.DataFrame) -> pd.DataFrame:
        out = frame.copy()
        out["ema"] = out["close"].ewm(span=EMA_SPAN, adjust=False).mean()
        out["atr"] = (out["high"] - out["low"]).rolling(ATR_BARS).mean()
        return out

    def entry_strategy(self, frame: pd.DataFrame, positions: tuple = ()) -> TradeOrder | None:
        if positions or len(frame) % ENTRY_EVERY:
            return None
        last = frame.iloc[-1]
        if last["close"] <= last["ema"]:
            return None
        return TradeOrder.buy(stop_loss=float(last["close"]) - STOP_POINTS)


class TrailingAmend(_Entries):
    """Moves the stop to each closed bar's low."""

    def exit_strategy(self, frame: pd.DataFrame, positions: tuple = ()) -> TradeOrder | None:
        if not positions:
            return None
        low = float(frame["low"].iloc[-1])
        current = positions[0].stop_loss
        if current is None or low > current:
            return TradeOrder.amend(stop_loss=low)
        return None


class TrailingPriced(_Entries):
    """Closes at the prior trailing level when a stored trade crosses it."""

    def exit_strategy(self, frame: pd.DataFrame, positions: tuple = (), *, ticks) -> TradeOrder | None:
        if not positions or len(frame) <= TRAIL_BARS:
            return None
        floor = float(frame["low"].iloc[-TRAIL_BARS - 1 : -1].min())
        stop = max(positions[0].entry_price - STOP_POINTS, floor)
        if frame["low"].iloc[-1] <= stop and (ticks.bar()["last"] <= stop).any():
            return TradeOrder.close(price=stop)
        return None


def time_run(store: TickStore, bars: pd.DataFrame, strategy: ResearchStrategy, workers: int) -> tuple[float, int]:
    start = time.perf_counter()
    result = backtest(bars, strategy=strategy, symbol=SYMBOL, ticks=store, day_trade=True, workers=workers)
    return time.perf_counter() - start, len(result.trades)


def run_benchmark(sessions: int, workers: int) -> dict[str, tuple[float, float, int]]:
    """Return ``{strategy: (seconds with workers=1, seconds with workers, trades)}``."""
    with tempfile.TemporaryDirectory() as tmp:
        store = write_sessions(Path(tmp) / "ticks", sessions)
        bars = store.bars(
            "M10", start=FIRST_DAY.isoformat(), end=(FIRST_DAY + timedelta(days=sessions - 1)).isoformat()
        )
        timings = {}
        for name, strategy_type in (("amend", TrailingAmend), ("priced", TrailingPriced)):
            serial, trades = time_run(store, bars, strategy_type(), 1)
            parallel, _ = time_run(store, bars, strategy_type(), workers)
            timings[name] = (serial, parallel, trades)
        return timings


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--sessions", type=int, default=60, help="Synthetic sessions to write and replay")
    parser.add_argument("--workers", type=int, default=4, help="Worker processes for the parallel runs")
    args = parser.parse_args(argv)
    if args.sessions < 1 or args.workers < 1:
        parser.error("--sessions and --workers must be positive")

    print(f"sessions: {args.sessions}, workers: {args.workers}")
    print(f"{'strategy':<10}{'workers=1 (s)':>16}{f'workers={args.workers} (s)':>18}{'trades':>9}")
    for name, (serial, parallel, trades) in run_benchmark(args.sessions, args.workers).items():
        print(f"{name:<10}{serial:>16.2f}{parallel:>18.2f}{trades:>9}")


if __name__ == "__main__":
    main()
