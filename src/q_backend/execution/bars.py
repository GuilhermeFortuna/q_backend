"""OHLCV frame helpers for forward execution (completed bars only)."""

from __future__ import annotations

from datetime import datetime
from typing import Iterable

import pandas as pd

from q_backend.market_data.exogenous_context import bar_duration
from q_backend.market_data.models import OHLCV

_OHLCV_COLUMNS = ("open", "high", "low", "close", "volume")


def ohlcv_list_to_frame(bars: Iterable[OHLCV]) -> pd.DataFrame:
    """Convert OHLCV models to a time-indexed OHLCV frame (index = bar open time)."""
    items = list(bars)
    if not items:
        return pd.DataFrame(columns=list(_OHLCV_COLUMNS))

    df = pd.DataFrame([bar.model_dump() for bar in items])
    df["time"] = pd.to_datetime(df["time"])
    df = df.set_index("time").sort_index()
    keep = [col for col in _OHLCV_COLUMNS if col in df.columns]
    return df[keep]


def bar_close_time(bar_open_time: datetime, timeframe: str) -> datetime:
    """Return the close timestamp for a bar that opens at ``bar_open_time``."""
    ts = pd.Timestamp(bar_open_time)
    if ts.tzinfo is None:
        close = ts + bar_duration(timeframe)
    else:
        close = ts + bar_duration(timeframe)
    return close.to_pydatetime()


def frame_close_times(index: pd.DatetimeIndex, timeframe: str) -> pd.DatetimeIndex:
    return index + bar_duration(timeframe)


def is_bar_complete(bar_open_time: datetime, timeframe: str, *, now: datetime) -> bool:
    """True when the bar that opened at ``bar_open_time`` has fully closed."""
    return bar_close_time(bar_open_time, timeframe) <= now


def drop_forming_bar(
    frame: pd.DataFrame,
    timeframe: str,
    *,
    now: datetime,
) -> pd.DataFrame:
    """Remove the last row when it is still the exchange's forming bar."""
    if frame.empty:
        return frame
    last_open = frame.index[-1]
    if is_bar_complete(last_open.to_pydatetime(), timeframe, now=now):
        return frame
    return frame.iloc[:-1]


def trim_rolling_window(frame: pd.DataFrame, max_bars: int) -> pd.DataFrame:
    if max_bars <= 0 or len(frame) <= max_bars:
        return frame
    return frame.iloc[-max_bars:]
