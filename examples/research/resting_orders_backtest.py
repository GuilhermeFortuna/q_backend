#!/usr/bin/env python3
"""Break-out with a resting buy stop and a trailing stop moved with ``amend``, over a temporary TickStore.

Each flat bar places a buy stop at its high, valid for the next bar only. The stop fills at the
first trade at or above that level. While the position is open, its stop trails the low of each
closed bar. The example writes a short synthetic session into a temporary directory, so it runs
offline. See docs/research-library.md for documentation.
"""

from __future__ import annotations

import argparse
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from q_backend.research import ResearchStrategy, TickStore, TradeOrder, backtest
from q_backend.research.positions import ResearchPosition
from q_backend.research.results import BacktestResult
from q_backend.research.tick_store import _slug_symbol, _write_day_atomic

SYMBOL = "WDO$N"
DAY = date(2026, 10, 5)
STOP_POINTS = 5.0

# Trade prices in stored order, as (HH:MM:SS, price), one list per M10 bar from 10:00.
SESSION = [
    [("10:00:00", 100.0), ("10:03:00", 101.0), ("10:06:00", 99.0), ("10:09:00", 100.0)],
    [("10:10:00", 100.0), ("10:12:00", 102.0), ("10:14:00", 104.0), ("10:16:00", 103.0)],
    [("10:20:00", 105.0), ("10:22:00", 106.0), ("10:24:00", 101.5), ("10:26:00", 103.0)],
    [("10:30:00", 103.0), ("10:32:00", 101.0), ("10:34:00", 102.0), ("10:36:00", 101.5)],
    [("10:40:00", 102.0), ("10:42:00", 102.5), ("10:44:00", 101.8), ("10:46:00", 102.0)],
]


class RestingBreakout(ResearchStrategy):
    """Buys a stop one point above each flat bar's high and trails the stop to each closed bar's low."""

    def entry_strategy(self, frame: pd.DataFrame, positions: tuple[ResearchPosition, ...] = ()) -> TradeOrder | None:
        if positions:
            return None
        last = frame.iloc[-1]
        return TradeOrder.buy_stop(float(last["high"]), stop_loss=float(last["low"]) - STOP_POINTS, expire_bars=1)

    def exit_strategy(self, frame: pd.DataFrame, positions: tuple[ResearchPosition, ...] = ()) -> TradeOrder | None:
        if not positions or positions[0].side != "long":
            return None
        low = float(frame["low"].iloc[-1])
        current = positions[0].stop_loss
        if current is None or low > current:
            return TradeOrder.amend(stop_loss=low)
        return None


def write_session(root: Path) -> TickStore:
    """Write the synthetic session under ``root`` and return its store."""
    rows = [row for bar in SESSION for row in bar]
    times = []
    for clock, _ in rows:
        hour, minute, second = (int(part) for part in clock.split(":"))
        # MT5 stores broker wall-clock time as the UTC components of the epoch.
        moment = datetime(DAY.year, DAY.month, DAY.day, hour, minute, second, tzinfo=timezone.utc)
        times.append(int(moment.timestamp() * 1000))
    prices = np.asarray([price for _, price in rows], dtype=np.float64)
    arrays = {
        "time_msc": np.asarray(times, dtype=np.int64),
        "bid": prices,
        "ask": prices,
        "last": prices,
        "volume": np.ones(len(rows), dtype=np.float64),
        "flags": np.full(len(rows), 32, dtype=np.int32),
    }
    directory = root / _slug_symbol(SYMBOL)
    directory.mkdir(parents=True, exist_ok=True)
    _write_day_atomic(directory / f"{DAY.isoformat()}.parquet", arrays)
    return TickStore(SYMBOL, root=root)


def run_example() -> BacktestResult:
    """Run the break-out over a session written to a temporary directory."""
    with tempfile.TemporaryDirectory() as directory:
        store = write_session(Path(directory) / "ticks")
        bars = store.bars("M10", start=DAY.isoformat())
        return backtest(bars, strategy=RestingBreakout(), symbol=SYMBOL, point_value=10.0, ticks=store)


def report(result: BacktestResult) -> None:
    trades = result.trades
    columns = ["entry_time", "entry_tick_time", "order_kind", "entry_price", "exit_tick_time", "exit_price"]
    columns += ["exit_reason", "stop_loss", "final_stop_loss"]
    print(trades[columns].to_string(index=False) if len(trades) else "no trades")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.parse_args(argv)
    report(run_example())


if __name__ == "__main__":
    main()
