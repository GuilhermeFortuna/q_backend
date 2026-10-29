"""Buy a breakout of the session's opening range, once a day, and exit before the close."""

from __future__ import annotations

import datetime as dt

import pandas as pd

from q_backend.research import IntParam, ResearchStrategy, TimeParam, TradeOrder

from ._lib.ticks import round_up_to_tick

TICK_SIZE = 5.0  # WIN trades in 5-point steps.


class SessionBreakout(ResearchStrategy):
    """Buy when a bar closes above the session's opening range, at most once per session.

    The opening range is the high of the first ``range_bars`` bars of each session. The entry
    level is that high rounded up to the instrument's tick, and the position closes at 16:55.
    """

    label = "Session breakout"
    parameters = {
        "range_bars": IntParam(min=1, max=24, label="Opening range bars"),
        "enter_after": TimeParam(label="Enter after"),
    }

    def __init__(self, range_bars: int = 12, enter_after: dt.time = dt.time(10, 0)):
        self.range_bars = range_bars
        self.enter_after = enter_after

    def compute_indicators(self, frame: pd.DataFrame) -> pd.DataFrame:
        out = frame.copy()
        session = frame.index.normalize()
        out["session_bar"] = frame.groupby(session).cumcount() + 1
        in_range = frame["high"].where(out["session_bar"] <= self.range_bars)
        out["entry_level"] = round_up_to_tick(in_range.groupby(session).cummax(), TICK_SIZE)
        return out

    def entry_strategy(self, frame: pd.DataFrame, positions=()):
        last = frame.iloc[-1]
        if positions or last["session_bar"] <= self.range_bars or pd.isna(last["entry_level"]):
            return None
        if frame.index[-1].time() < self.enter_after:
            return None
        if last["close"] > last["entry_level"]:
            return TradeOrder.buy()
        return None

    def exit_strategy(self, frame: pd.DataFrame, positions=()):
        if positions and frame.index[-1].time() >= dt.time(16, 55):
            return TradeOrder.close()
        return None
