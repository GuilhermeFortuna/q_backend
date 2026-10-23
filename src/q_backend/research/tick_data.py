"""Tick data for research strategy hooks: a candle's or a range's ticks, never past the hook's cutoff."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import numpy as np
import pandas as pd

from q_backend.market_data.clients.shared import _time_msc_to_naive_local
from q_backend.market_data.timezone import BRASILIA_TZ
from q_backend.research.data import _ticks_frame_from_columnar
from q_backend.research.errors import NoMarketDataError
from q_backend.research.frame import bounds_to_brasilia_naive, parse_query_bound
from q_backend.research.intrabar import bar_end
from q_backend.research.tick_store import TICK_STORE_SOURCE, TickStore, naive_local_to_time_msc

_COLUMNS = ("time_msc", "bid", "ask", "last", "volume", "flags")
_DTYPES = {"time_msc": np.int64, "bid": np.float64, "ask": np.float64, "last": np.float64, "volume": np.float64}


class TickSource:
    """One run's tick reader: its store and the one session it has read, shared by the run's ``TickData``.

    Pickles without the cached session, so each worker process reads its own.
    """

    def __init__(self, store: TickStore) -> None:
        self.store = store
        self._day: date | None = None
        self._columns: dict[str, np.ndarray] | None = None

    def __getstate__(self) -> dict[str, Any]:
        state = self.__dict__.copy()
        state["_day"] = None
        state["_columns"] = None
        return state

    def session(self, day: date) -> dict[str, np.ndarray]:
        """One session's columns; raises ``NoMarketDataError`` when the session is not stored."""
        if day != self._day:
            if self.store.session_path(day) is None:
                raise NoMarketDataError(
                    symbol=self.store.symbol,
                    timeframe="ticks",
                    source=TICK_STORE_SOURCE,
                    start=day.isoformat(),
                    end=(day + timedelta(days=1)).isoformat(),
                    hint=f"No stored ticks for session {day.isoformat()}; run TickStore.sync for that day.",
                )
            self._columns = self.store._session_columns(day)
            self._day = day
        assert self._columns is not None
        return self._columns


def _msc(moment: pd.Timestamp) -> int:
    """The stored ``time_msc`` of an instant, the first one at or after it."""
    return naive_local_to_time_msc(bounds_to_brasilia_naive(moment, moment)[0])


def _moment(time_msc: int) -> pd.Timestamp:
    return pd.Timestamp(_time_msc_to_naive_local(int(time_msc))).tz_localize(BRASILIA_TZ)


def _session_day(moment: pd.Timestamp) -> date:
    return moment.tz_convert(BRASILIA_TZ).date()


def _empty_frame() -> pd.DataFrame:
    return _ticks_frame_from_columnar({key: np.empty(0, dtype=_DTYPES.get(key, np.int32)) for key in _COLUMNS})


class TickData:
    """The ticks a hook may read, up to its cutoff.

    Hooks receive instances from the engine. ``bar`` and ``between`` read a session only when
    called, and every hook sees ticks only through the end of its current bar.
    """

    def __init__(
        self,
        source: TickSource,
        index: pd.DatetimeIndex,
        current: int,
    ) -> None:
        self._source = source
        self._index = index
        self._current = current
        self._cutoff = bar_end(index, current)

    @property
    def cutoff(self) -> pd.Timestamp:
        """The latest moment this instance can return ticks for."""
        return self._cutoff

    def bar(self, timestamp: Any = None) -> pd.DataFrame:
        """The stored ticks of the candle that opens at ``timestamp``; ``None`` means the current bar."""
        position = self._current if timestamp is None else self._position_of(timestamp)
        cutoff = self._cutoff
        start = self._index[position]
        if start >= cutoff:
            raise ValueError(f"The bar at {start} opens at or after the cutoff {cutoff}; its ticks are not yet known")
        day = _session_day(start)
        columns = self._source.session(day)
        time_msc = columns["time_msc"]
        start_msc, end_msc = (_msc(moment) for moment in (start, bar_end(self._index, position)))
        lower = int(np.searchsorted(time_msc, start_msc, "left"))
        upper = min(int(np.searchsorted(time_msc, end_msc, "left")), self._visible(day, time_msc))
        return self._frame(columns, lower, max(lower, upper))

    def between(self, start: Any, end: Any) -> pd.DataFrame:
        """The stored ticks in ``[start, end)``, across sessions, with ``end`` clipped to the cutoff."""
        cutoff = self._cutoff
        start_ts = parse_query_bound(start)
        end_ts = parse_query_bound(end)
        if start_ts >= cutoff:
            raise ValueError(f"between() starts at {start_ts}, at or after the cutoff {cutoff}")
        if start_ts > end_ts:
            raise ValueError("start must be <= end")
        end_ts = min(end_ts, cutoff)
        pieces: list[pd.DataFrame] = []
        first, last = _session_day(start_ts), _session_day(end_ts)
        for day in (first + timedelta(days=k) for k in range((last - first).days + 1)):
            if not self._has_bars(day):
                continue
            columns = self._source.session(day)
            time_msc = columns["time_msc"]
            lower = int(np.searchsorted(time_msc, _msc(start_ts), "left"))
            upper = min(int(np.searchsorted(time_msc, _msc(end_ts), "left")), self._visible(day, time_msc))
            if upper > lower:
                pieces.append(self._frame(columns, lower, upper))
        if not pieces:
            return _empty_frame()
        return pd.concat(pieces)

    def _visible(self, day: date, time_msc: np.ndarray) -> int:
        """How many leading rows of a session are at or before the cutoff."""
        cutoff = self._cutoff
        cutoff_day = _session_day(cutoff)
        if day < cutoff_day:
            return len(time_msc)
        if day > cutoff_day:
            return 0
        return int(np.searchsorted(time_msc, _msc(cutoff), "left"))

    def _has_bars(self, day: date) -> bool:
        """Whether the backtest frame has a bar in this calendar day; other days have no ticks to read."""
        day_start = pd.Timestamp(day).tz_localize(BRASILIA_TZ)
        day_end = day_start + pd.Timedelta(days=1)
        return int(self._index.searchsorted(day_start)) < int(self._index.searchsorted(day_end))

    def _position_of(self, timestamp: Any) -> int:
        moment = pd.Timestamp(timestamp)
        moment = moment.tz_localize(BRASILIA_TZ) if moment.tzinfo is None else moment
        found = int(self._index.get_indexer([moment])[0])
        if found < 0:
            raise ValueError(f"{moment} is not a bar of the backtest frame")
        return found

    @staticmethod
    def _frame(columns: dict[str, np.ndarray], lower: int, upper: int) -> pd.DataFrame:
        arrays = {key: columns[key][lower:upper].copy() for key in _COLUMNS}
        frame = _ticks_frame_from_columnar(arrays)
        frame.attrs = {}
        return frame
