"""Candle trade prices for intrabar stops, targets and custom exits, served by q_core (Q-104, Q-121)."""

from __future__ import annotations

import numpy as np
import pandas as pd

from q_backend.backtesting.candle_kernel import engine
from q_backend.market_data.timezone import BRASILIA_TZ
from q_backend.research.errors import NoMarketDataError
from q_backend.research.frame import bounds_to_brasilia_naive
from q_backend.research.tick_store import TICK_STORE_SOURCE, TickStore, naive_local_to_time_msc


def bar_end(index: pd.DatetimeIndex, bar: int) -> pd.Timestamp:
    """A candle spans ``[index[i], index[i + 1])`` in the same session, and otherwise to the session end."""
    start = index[bar]
    if bar + 1 < len(index):
        following = index[bar + 1]
        if following.tz_convert(BRASILIA_TZ).date() == start.tz_convert(BRASILIA_TZ).date():
            return following
    return start.tz_convert(BRASILIA_TZ).normalize() + pd.Timedelta(days=1)


def native_tick_source(store: TickStore, chunk: pd.DataFrame):  # -> q_core.engine.SessionTickSource
    """Serve each candle's trade prices from the store's session files, without a Python call per bar.

    Each candle's interval is the one ``TickReplay`` used, in the store's ``time_msc`` units. A
    session that is not stored is passed as ``None`` and fails only when a candle needs its prices.
    """
    index = chunk.index
    sessions = [start.tz_convert(BRASILIA_TZ).date() for start in index]
    days = sorted(set(sessions))
    ordinal = {day: position for position, day in enumerate(days)}
    starts: list[int] = []
    ends: list[int] = []
    for bar in range(len(index)):
        start_naive, end_naive = bounds_to_brasilia_naive(index[bar], bar_end(index, bar))
        starts.append(naive_local_to_time_msc(start_naive))
        ends.append(naive_local_to_time_msc(end_naive))
    paths = [None if (path := store.session_path(day)) is None else str(path) for day in days]
    return engine.SessionTickSource(
        paths,
        np.asarray([ordinal[day] for day in sessions], dtype=np.int64),
        np.asarray(starts, dtype=np.int64),
        np.asarray(ends, dtype=np.int64),
        np.ascontiguousarray(chunk["open"].to_numpy(dtype=np.float64)),
        np.ascontiguousarray(chunk["high"].to_numpy(dtype=np.float64)),
        np.ascontiguousarray(chunk["low"].to_numpy(dtype=np.float64)),
        np.ascontiguousarray(chunk["close"].to_numpy(dtype=np.float64)),
    )


def intrabar_error(exc: Exception, chunk: pd.DataFrame, store: TickStore) -> Exception:
    """Translate a q_core intrabar failure into the research errors, naming the candle that needed ticks."""
    bar = exc.bar
    start = chunk.index[bar]
    end = bar_end(chunk.index, bar)
    if exc.reason == "missing_session":
        session = start.tz_convert(BRASILIA_TZ).date()
        return NoMarketDataError(
            symbol=store.symbol,
            timeframe="ticks",
            source=TICK_STORE_SOURCE,
            start=start.isoformat(),
            end=end.isoformat(),
            hint=f"No stored ticks for session {session.isoformat()}; run TickStore.sync for that day.",
        )
    if exc.reason == "empty":
        return NoMarketDataError(
            symbol=store.symbol,
            timeframe="ticks",
            source=TICK_STORE_SOURCE,
            start=start.isoformat(),
            end=end.isoformat(),
            hint=f"No stored trade prices for bar {bar} ({start.isoformat()}); run TickStore.sync.",
        )
    return ValueError(
        f"Bar {bar} at {start.isoformat()} does not match its stored ticks "
        f"(first/high/low/last {exc.observed} versus open/high/low/close {exc.expected}); "
        "build the frame with TickStore.bars from the same store"
    )
