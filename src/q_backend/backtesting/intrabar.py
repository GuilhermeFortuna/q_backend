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

# MT5 tick flags that mark a deal. Quote-only updates repeat the previous ``last``, so a
# candle's first "price" would be the prior candle's close instead of its open.
_TICK_FLAG_LAST = 8
_TICK_FLAG_VOLUME = 16
_TICK_FLAG_BUY = 32
_TICK_FLAG_SELL = 64
_TRADE_FLAGS = _TICK_FLAG_LAST | _TICK_FLAG_VOLUME | _TICK_FLAG_BUY | _TICK_FLAG_SELL


class MarketTickReplay:
    """Read only screened candles, retaining provider tick order at equal timestamps.

    OHLC must match the returned trade prices. Price-adjusted bars, empty history and
    incomplete candles fail instead of inventing an intrabar path from OHLC extremes.
    """

    def __init__(
        self,
        service: Any,
        symbol: str,
        timeframe: str,
        frame: pd.DataFrame,
        tick_size: float | None = None,
    ) -> None:
        self._service = service
        self._tick_size = tick_size if tick_size is not None and np.isfinite(tick_size) and tick_size > 0 else None
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
        if "flags" in arrays:
            flags = np.asarray(arrays["flags"], dtype=np.int64)
            if flags.shape == times.shape:
                mask &= (flags & _TRADE_FLAGS) != 0
        times, prices = times[mask], prices[mask]
        order = np.argsort(times, kind="stable")
        times, prices = times[order], prices[order]
        if self._tick_size is not None:
            # Terminal ticks are rounded to the symbol's decimals (4513.25 arrives as
            # 4513.3); the instrument trades on its tick grid, so snap back onto it.
            prices = np.round(prices / self._tick_size) * self._tick_size
        return times, prices

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
        tolerance = 0.0 if self._tick_size is None else self._tick_size * 1e-6
        if not np.allclose(observed, expected, rtol=0.0, atol=tolerance):
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
    lookup = getattr(service, "get_symbol_info", None)
    grid = None
    if lookup is not None:
        info = lookup(strategy.symbol)
        value = float((info or {}).get("trade_tick_size") or 0.0)
        grid = value if np.isfinite(value) and value > 0 else None
    tick_size = None
    if float(params["breakeven_emergency_stop_ticks"]) > 0:
        if grid is None:
            raise ValueError(
                f"{strategy.symbol}: Breakeven Emergency Stop requires a finite positive instrument trade_tick_size"
            )
        tick_size = grid
    return {
        "tick_size": tick_size,
        "intrabar_factory": lambda frame: MarketTickReplay(service, strategy.symbol, timeframe, frame, grid),
    }
