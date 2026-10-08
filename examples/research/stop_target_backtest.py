#!/usr/bin/env python3
"""Breakout with stop and target orders and a phase-aware exit, confirmed from a TickStore.

The entry carries its stop and target. The exit screens each candle on its low and confirms
with the trade price of a tick, so a candle is replayed only when the screen qualifies.
Requires the sessions of the backtest window on disk; see sync_ticks.py.
See docs/research-library.md for documentation.
"""

from __future__ import annotations

import argparse
from collections import Counter
from pathlib import Path

import pandas as pd

from q_backend.research import ResearchStrategy, TickStore, TradeOrder, backtest
from q_backend.research.results import BacktestResult

BREAKOUT_BARS = 20
ATR_BARS = 14
EXIT_BARS = 10
STOP_ATR = 4.0
TARGET_ATR = 6.0


class StopTargetBreakout(ResearchStrategy):
    """Buys a close above the prior 20-bar high, with ATR stop and target, and exits below a 10-bar low."""

    def compute_indicators(self, frame: pd.DataFrame) -> pd.DataFrame:
        out = frame.copy()
        out["upper"] = out["high"].rolling(BREAKOUT_BARS).max().shift(1)
        out["atr"] = (out["high"] - out["low"]).rolling(ATR_BARS).mean()
        out["exit_floor"] = out["low"].rolling(EXIT_BARS).min().shift(1)
        return out

    def entry_strategy(self, frame: pd.DataFrame) -> TradeOrder | None:
        last = frame.iloc[-1]
        if not (last["close"] > last["upper"]) or not (last["atr"] > 0):
            return None
        return TradeOrder.buy(
            stop_loss=float(last["close"] - STOP_ATR * last["atr"]),
            take_profit=float(last["close"] + TARGET_ATR * last["atr"]),
        )

    def exit_strategy(self, frame: pd.DataFrame, positions: tuple = (), *, phase: str = "bar") -> TradeOrder | None:
        if phase == "bar" or not positions:
            return None
        floor = frame["exit_floor"].iloc[-1]
        if phase == "screen":
            reached = frame["low"].iloc[-1] <= floor
        else:
            reached = frame["close"].iloc[-1] <= floor
        return TradeOrder.close() if reached else None


def run_stop_target_backtest(
    store: TickStore,
    *,
    symbol: str,
    start: str,
    end: str | None = None,
    point_value: float = 10.0,
) -> BacktestResult:
    bars = store.bars("M10", start=start, end=end)
    return backtest(
        bars,
        strategy=StopTargetBreakout(),
        symbol=symbol,
        point_value=point_value,
        ticks=store,
    )


def report(result: BacktestResult) -> None:
    trades = result.trades
    print("metrics:", result.metrics)
    print("exit reasons:", dict(Counter(trades["exit_reason"].dropna())))
    exited_in_candle = trades["exit_tick_time"].notna()
    print(f"trades: {len(trades)}, closed inside a candle at a tick: {int(exited_in_candle.sum())}")
    if exited_in_candle.any():
        print(trades.loc[exited_in_candle, ["entry_time", "exit_time", "exit_tick_time", "exit_price", "exit_reason"]])
    print(f"rejected entries: {len(result.rejected_entries)}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--symbol", default="WDO$N")
    parser.add_argument("--start", required=True, help="First session day, e.g. 2025-10-01")
    parser.add_argument("--end", default=None, help="Last session day (inclusive); default: all stored")
    parser.add_argument("--root", type=Path, default=None, help="TickStore root (Q_RESEARCH_TICK_STORE)")
    parser.add_argument("--point-value", type=float, default=10.0)
    args = parser.parse_args()

    store = TickStore(args.symbol, root=args.root)
    report(
        run_stop_target_backtest(
            store,
            symbol=args.symbol,
            start=args.start,
            end=args.end,
            point_value=args.point_value,
        )
    )


if __name__ == "__main__":
    main()
