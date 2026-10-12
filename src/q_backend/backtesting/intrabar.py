"""Lazy, validated trade-price replay for service-backed candle backtests."""

from __future__ import annotations

import time
from typing import Any

import numpy as np
import pandas as pd

from q_backend.market_data.clients.metatrader import _naive_local_to_time_msc, _to_naive_local
from q_backend.market_data.clients.shared import COPY_TICKS_ALL
from q_backend.market_data.exogenous_context import bar_duration
from q_backend.backtesting.candle_kernel import _exit_params_from_strategy

# MT5 answers an empty array for history it has not synchronized yet and serves the
# same request moments later, so an empty candle is re-read before it is reported.
_EMPTY_READ_ATTEMPTS = 3
_EMPTY_READ_DELAY_SECONDS = 1.0


class MarketTickReplay:
    """Read only screened candles, retaining provider tick order at equal timestamps.

    OHLC must match the returned trade prices. Price-adjusted bars, empty history and
    incomplete candles fail instead of inventing an intrabar path from OHLC extremes.
    """

    def __init__(self, service: Any, symbol: str, timeframe: str, frame: pd.DataFrame) -> None:
        self._service = service
        self._symbol = symbol
        self._duration = bar_duration(timeframe)
        self._frame = frame
        self._last: tuple[int, np.ndarray, np.ndarray] | None = None

    def _read_trades(self, start: Any, end: Any) -> tuple[np.ndarray, np.ndarray]:
        arrays = self._service.get_ticks_columnar(self._symbol, start, end, flags=COPY_TICKS_ALL)
        times = np.asarray(arrays["time_msc"], dtype=np.int64)
        prices = np.asarray(arrays["last"], dtype=np.float64)
        if times.ndim != 1 or prices.ndim != 1 or times.size != prices.size:
            raise ValueError(f"{self._symbol}: tick times and prices must be equal-length vectors")
        mask = (
            (times >= _naive_local_to_time_msc(start))
            & (times < _naive_local_to_time_msc(end))
            & np.isfinite(prices)
            & (prices > 0)
        )
        times, prices = times[mask], prices[mask]
        order = np.argsort(times, kind="stable")
        return times[order], prices[order]

    def __call__(self, bar: int) -> tuple[np.ndarray, np.ndarray]:
        if self._last is not None and self._last[0] == bar:
            return self._last[1], self._last[2]
        start = _to_naive_local(self._frame.index[bar].to_pydatetime())
        end = start + self._duration.to_pytimedelta()
        if bar + 1 < len(self._frame):
            end = min(end, _to_naive_local(self._frame.index[bar + 1].to_pydatetime()))
        times, prices = self._read_trades(start, end)
        for _ in range(_EMPTY_READ_ATTEMPTS - 1):
            if prices.size:
                break
            time.sleep(_EMPTY_READ_DELAY_SECONDS)
            times, prices = self._read_trades(start, end)
        if prices.size == 0:
            raise ValueError(
                f"{self._symbol}: no historical trade ticks for candle {start}; load tick history before running intrabar Breakeven Stop"
            )
        expected = tuple(float(self._frame.iloc[bar][col]) for col in ("open", "high", "low", "close"))
        observed = (prices[0], prices.max(), prices.min(), prices[-1])
        if observed != expected:
            raise ValueError(
                f"{self._symbol}: historical trade ticks do not reproduce OHLC for candle {start}: {observed} versus {expected}; use bars and ticks from the same unadjusted history"
            )
        self._last = (bar, np.ascontiguousarray(times * 1000), np.ascontiguousarray(prices))
        return self._last[1], self._last[2]


def breakeven_market_inputs(strategy: Any, service: Any, timeframe: str) -> dict[str, Any]:
    """Resolve instrument metadata only for an enabled breakeven rule."""
    params = _exit_params_from_strategy(strategy)
    if float(params.get("breakeven_trigger_pct", 0.0)) <= 0.0:
        return {}
    if service is None:
        from q_backend.market_data.service import MarketDataService

        service = MarketDataService()
    tick_size = None
    if float(params["breakeven_emergency_stop_ticks"]) > 0:
        info = service.get_symbol_info(strategy.symbol)
        tick_size = float((info or {}).get("trade_tick_size") or 0.0)
        if not np.isfinite(tick_size) or tick_size <= 0:
            raise ValueError(
                f"{strategy.symbol}: Breakeven Emergency Stop requires a finite positive instrument trade_tick_size"
            )
    return {
        "tick_size": tick_size,
        "intrabar_factory": lambda frame: MarketTickReplay(service, strategy.symbol, timeframe, frame),
    }
