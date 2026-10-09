"""Scratch: SmartMaCrossover-style strategy with tick-confirmed trailing Donchian-style stops.

Longs exit when price hits the N-bar low floor; shorts when price hits the N-bar high ceiling.
Based on experiments/ccm_test.py. Uses TickStore.bars(..., sync=True) to pull missing
sessions from the MT5 gateway (./dev gateway or full stack must be reachable).

Run from q_backend:
  uv run python experiments/ccm_trailing_stop_ticks_scratch.py
"""

from __future__ import annotations

from datetime import datetime

import pandas as pd
from dotenv import load_dotenv

from q_backend.research import (
    ChartIndicator,
    ResearchStrategy,
    TickStore,
    TradeOrder,
    backtest,
    indicators,
)

load_dotenv()

SYMBOL = "WDO$N"
TIMEFRAME = "M10"
START_DATE = "2026-06-01"
END_DATE = datetime.now().strftime("%Y-%m-%d")


class SmartMaCrossoverTrailingStop(ResearchStrategy):
    def __init__(self, short_ma_period: int = 9, long_ma_period: int = 20, trail_bars: int = 5):
        self.short_ma_period = short_ma_period
        self.long_ma_period = long_ma_period
        self.short_ma_type = "ema"
        self.long_ma_type = "wma"
        self.trail_bars = trail_bars

    def compute_indicators(self, frame: pd.DataFrame) -> pd.DataFrame:
        frame = frame.copy()
        frame["highest"] = frame["high"].rolling(window=self.trail_bars).max()
        frame["lowest"] = frame["low"].rolling(window=self.trail_bars).min()
        # Trail levels from prior bars only (fixed for the bar at open).
        frame["short_trail_stop"] = frame["highest"].shift(1)
        frame["long_trail_stop"] = frame["lowest"].shift(1)

        frame["short_ma"] = indicators.ma(frame["close"], self.short_ma_period, kind=self.short_ma_type)
        frame["short_ma_diff"] = frame["short_ma"].diff(1)
        frame["long_ma"] = indicators.ma(frame["close"], self.long_ma_period, kind=self.long_ma_type)
        frame["long_ma_diff"] = frame["long_ma"].diff(1)
        frame["ma_delta"] = frame["short_ma"] - frame["long_ma"]
        return frame

    def chart_indicators(self):
        return [
            ChartIndicator("short_ma", label="Short MA"),
            ChartIndicator("long_ma", label="Long MA"),
            ChartIndicator("highest", label="Highest"),
            ChartIndicator("short_trail_stop", label="Short trail stop"),
            ChartIndicator("long_trail_stop", label="Long trail stop"),
        ]

    def entry_strategy(self, frame, positions):
        if positions or len(frame) < 2 or frame.iloc[-1].isna().any():
            return None

        short_ma_diff = frame["short_ma_diff"].iat[-1]
        long_ma_diff = frame["long_ma_diff"].iat[-1]
        ma_delta = frame["ma_delta"].iat[-1]
        prev_ma_delta = frame["ma_delta"].iat[-2]

        ma_going_up = short_ma_diff > 0 and long_ma_diff > 0 and ma_delta > 0
        ma_going_down = short_ma_diff < 0 and long_ma_diff < 0 and ma_delta < 0
        ma_cross_up = prev_ma_delta < 0 and ma_delta > 0
        ma_cross_down = prev_ma_delta > 0 and ma_delta < 0

        if ma_going_up and ma_cross_up:
            return TradeOrder.buy()
        if ma_going_down and ma_cross_down:
            return TradeOrder.sell()
        return None

    def exit_strategy(self, frame, positions=(), *, phase: str = "bar"):
        if not positions or len(frame) < 2:
            return None

        position = positions[0]

        # Trailing N-bar stops (tick-accurate when ticks= is passed).
        if phase in ("screen", "tick"):
            if position.side == "short":
                level = frame["short_trail_stop"].iat[-1]
                if pd.notna(level):
                    if phase == "screen":
                        hit = frame["high"].iat[-1] >= level
                    else:
                        hit = frame["close"].iat[-1] >= level
                    if hit:
                        return TradeOrder.close()
            elif position.side == "long":
                level = frame["long_trail_stop"].iat[-1]
                if pd.notna(level):
                    if phase == "screen":
                        hit = frame["low"].iat[-1] <= level
                    else:
                        hit = frame["close"].iat[-1] <= level
                    if hit:
                        return TradeOrder.close()

        # Other exits: closed-bar only (fill at next open).
        if phase != "bar":
            return None

        if frame.iloc[-1].isna().any():
            return None

        ma_delta = frame["ma_delta"].iat[-1]
        prev_ma_delta = frame["ma_delta"].iat[-2]
        ma_cross_up = prev_ma_delta < 0 and ma_delta > 0
        ma_cross_down = prev_ma_delta > 0 and ma_delta < 0
        if ma_cross_up or ma_cross_down:
            return TradeOrder.close()

        window_ok = (
            not frame["highest"].iloc[-self.trail_bars :].isna().any()
            and not frame["lowest"].iloc[-self.trail_bars :].isna().any()
        )
        if not window_ok:
            return None

        top_break = (
            frame["highest"].iat[-1] == frame["high"].iat[-1] and frame["highest"].iat[-1] > frame["highest"].iat[-2]
        )
        bottom_break = (
            frame["lowest"].iat[-1] == frame["low"].iat[-1] and frame["lowest"].iat[-1] < frame["lowest"].iat[-2]
        )

        if position.side == "long" and bottom_break:
            return TradeOrder.close()
        if position.side == "short" and top_break:
            return TradeOrder.close()

        return None


def main() -> None:
    store = TickStore(SYMBOL)
    # Tick-built bars have no spread; an all-NaN column would trip the strategy's isna() guards.
    bars = store.bars(TIMEFRAME, start=START_DATE, end=END_DATE, sync=True).drop(columns="spread")

    result = backtest(
        bars,
        strategy=SmartMaCrossoverTrailingStop(trail_bars=5),
        symbol=SYMBOL,
        quantity=1,
        point_value=10.0,
        initial_capital=5000.0,
        ticks=store,
        workers="auto",
    )

    trades = result.trades
    cols = ["side", "entry_time", "exit_time", "exit_tick_time", "exit_price", "exit_reason"]
    print(trades[cols].to_string())
    print("\nmetrics:", result.metrics)
    # result.publish()


if __name__ == "__main__":
    main()
    pass
