"""Lazy candle price source for intrabar stops, targets and custom exits (Q-104)."""

from __future__ import annotations

import numpy as np
import pandas as pd

from q_backend.market_data.timezone import BRASILIA_TZ
from q_backend.research.errors import NoMarketDataError
from q_backend.research.tick_store import TICK_STORE_SOURCE, TickStore


class TickReplay:
    """Serves one candle's trade prices from a TickStore when the kernel asks for them.

    A candle spans ``[index[i], index[i + 1])`` when the next bar is in the same
    session, and otherwise runs to the end of its exchange calendar day. The
    frame must be built from the same store with ``TickStore.bars``, so the
    loaded prices must reproduce that candle's open, high, low and close.
    """

    def __init__(self, store: TickStore, chunk: pd.DataFrame) -> None:
        self._store = store
        self._index = chunk.index
        self._open = chunk["open"].to_numpy(dtype=np.float64)
        self._high = chunk["high"].to_numpy(dtype=np.float64)
        self._low = chunk["low"].to_numpy(dtype=np.float64)
        self._close = chunk["close"].to_numpy(dtype=np.float64)

    def __call__(self, bar: int) -> tuple[np.ndarray, np.ndarray]:
        start = self._index[bar]
        end = self._interval_end(bar)
        session = start.tz_convert(BRASILIA_TZ).date()
        if session not in set(self._store.sessions()):
            raise NoMarketDataError(
                symbol=self._store.symbol,
                timeframe="ticks",
                source=TICK_STORE_SOURCE,
                start=start.isoformat(),
                end=end.isoformat(),
                hint=f"No stored ticks for session {session.isoformat()}; run TickStore.sync for that day.",
            )
        times, prices = self._store.trade_prices(start, end)
        if prices.size == 0:
            raise NoMarketDataError(
                symbol=self._store.symbol,
                timeframe="ticks",
                source=TICK_STORE_SOURCE,
                start=start.isoformat(),
                end=end.isoformat(),
                hint=f"No stored trade prices for bar {bar} ({start.isoformat()}); run TickStore.sync.",
            )
        self._check_matches_bar(bar, prices)
        return times, prices

    def _interval_end(self, bar: int) -> pd.Timestamp:
        start = self._index[bar]
        if bar + 1 < len(self._index):
            following = self._index[bar + 1]
            if following.tz_convert(BRASILIA_TZ).date() == start.tz_convert(BRASILIA_TZ).date():
                return following
        return start.tz_convert(BRASILIA_TZ).normalize() + pd.Timedelta(days=1)

    def _check_matches_bar(self, bar: int, prices: np.ndarray) -> None:
        observed = (prices[0], prices.max(), prices.min(), prices[-1])
        expected = (self._open[bar], self._high[bar], self._low[bar], self._close[bar])
        if observed != expected:
            raise ValueError(
                f"Bar {bar} at {self._index[bar].isoformat()} does not match its stored ticks "
                f"(first/high/low/last {observed} versus open/high/low/close {expected}); "
                "build the frame with TickStore.bars from the same store"
            )
