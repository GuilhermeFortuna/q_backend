"""Closed-bar context: coarser timeframes read alongside a finer decision frame, without look-ahead."""

from __future__ import annotations

import numpy as np
import pandas as pd

from q_backend.market_data.clients.metatrader import TIMEFRAME_NAMES
from q_backend.market_data.exogenous_context import bar_duration
from q_backend.market_data.timezone import BRASILIA_TZ
from q_backend.research.frame import FRAME_COLUMNS, normalize_timeframe
from q_backend.research.tick_store import _aggregate_bars

_CONTEXT_COLUMNS: tuple[str, ...] = ("open", "high", "low", "close", "tick_volume")


def closed_bars(frame: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    """Coarse OHLCV bars aligned to ``frame``'s index, each holding only bars that have closed by then.

    The coarse bars are aggregated per B3 session, as :meth:`TickStore.bars` aggregates them. A row
    takes the last coarse bar whose close is at or before the row's own close, so a row inside a
    forming coarse bar takes the coarse bar before it. Rows before the first coarse bar close are
    missing.

    Parameters
    ----------
    frame : pandas.DataFrame
        Research bars from :func:`load_bars` or :meth:`TickStore.bars`, indexed by bar open time.
    timeframe : str
        The coarser timeframe, for example ``"M5"`` on an M1 frame. It must be a multiple of the
        frame's timeframe.

    Returns
    -------
    pandas.DataFrame
        Columns ``open``, ``high``, ``low``, ``close`` and ``tick_volume`` of the coarse bar, indexed
        like ``frame``. Join it back to the frame, and compute indicators on it, to read coarse bars
        without looking ahead.

    Raises
    ------
    ValueError
        If the frame has no timeframe, ``timeframe`` is not a multiple of the frame's, or the frame
        lacks research columns.
    """
    coarse = normalize_timeframe(timeframe)
    fine = _frame_timeframe(frame)
    fine_minutes = bar_duration(fine) // pd.Timedelta(minutes=1)
    coarse_minutes = bar_duration(coarse) // pd.Timedelta(minutes=1)
    if coarse_minutes < fine_minutes or coarse_minutes % fine_minutes:
        raise ValueError(f"closed_bars: {coarse} is not a multiple of the frame's {fine}")
    missing = [column for column in FRAME_COLUMNS if column not in frame.columns]
    if missing:
        raise ValueError(f"closed_bars needs research bars; missing columns {missing}")
    if not frame.index.is_monotonic_increasing or frame.index.has_duplicates:
        raise ValueError("closed_bars needs a frame indexed by strictly increasing bar open times")

    if frame.empty:
        return pd.DataFrame(index=frame.index, columns=list(_CONTEXT_COLUMNS), dtype="float64")

    coarse_frame = _session_bars(frame, coarse)
    coarse_closes = _epoch_ns(coarse_frame.index + bar_duration(coarse))
    row_closes = _epoch_ns(frame.index + bar_duration(fine))
    # Index of the last coarse bar that closed at or before each row's close; -1 before the first one.
    last_closed = np.searchsorted(coarse_closes, row_closes, side="right") - 1

    values = coarse_frame[list(_CONTEXT_COLUMNS)].to_numpy(dtype="float64")
    taken = np.full((len(frame), len(_CONTEXT_COLUMNS)), np.nan)
    has_bar = last_closed >= 0
    taken[has_bar] = values[last_closed[has_bar]]
    return pd.DataFrame(taken, index=frame.index, columns=list(_CONTEXT_COLUMNS))


def _frame_timeframe(frame: pd.DataFrame) -> str:
    """The frame's own timeframe: recorded by the loaders, or else the smallest bar spacing.

    Strategy hooks receive frames without their attributes, so the spacing is the fallback there.
    """
    recorded = frame.attrs.get("q_research", {}).get("timeframe")
    if recorded is not None:
        return normalize_timeframe(str(recorded))
    if len(frame.index) < 2:
        raise ValueError("closed_bars cannot infer the timeframe of a frame with fewer than two bars")
    spacing = (frame.index[1:] - frame.index[:-1]).min()
    for name in TIMEFRAME_NAMES:
        try:
            if bar_duration(name) == spacing:
                return name
        except ValueError:
            continue
    raise ValueError(f"closed_bars cannot match the bar spacing {spacing} to a supported timeframe")


def _epoch_ns(index: pd.DatetimeIndex) -> np.ndarray:
    """UTC epoch nanoseconds, whatever resolution the index was built with."""
    return index.as_unit("ns").asi8


def _session_bars(frame: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    """Aggregate each B3 session separately, so no coarse bar spans two sessions."""
    local = frame.index.tz_convert(BRASILIA_TZ)
    sessions = np.asarray(local.date)
    parts = [_aggregate_bars(frame.loc[sessions == day], timeframe) for day in dict.fromkeys(sessions)]
    if not parts:
        return frame.iloc[0:0]
    return pd.concat(parts).sort_index(kind="stable")
