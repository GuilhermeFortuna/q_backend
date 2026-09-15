from __future__ import annotations

import pandas as pd

from q_backend.backtesting.indicator_kernels import (
    as_float64,
    kernels,
    require_window,
    shared_index,
    to_series,
)


def compute_realized_vol(close: pd.Series, window: int, periods_per_year: int = 252) -> pd.Series:
    """Rolling annualized close-to-close volatility from log returns."""
    require_window("window", window, 1)
    res = kernels.realized_vol(as_float64(close), window, periods_per_year)
    return to_series(res, close.index, close.name)


def compute_yang_zhang(
    open_: pd.Series,
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    window: int,
    periods_per_year: int = 252,
) -> pd.Series:
    """Rolling annualized Yang–Zhang (2000) volatility. Causal: value at bar i
    uses bars <= i only."""
    require_window("window", window, 2)
    idx = shared_index(close, open_, high, low)
    res = kernels.yang_zhang(
        as_float64(open_),
        as_float64(high),
        as_float64(low),
        as_float64(close),
        window,
        periods_per_year,
    )
    return to_series(res, idx, None)


def compute_rsi(close: pd.Series, period: int) -> pd.Series:
    require_window("period", period, 1)
    res = kernels.rsi(as_float64(close), period)
    return to_series(res, close.index, close.name)


def compute_bollinger_bands(close: pd.Series, period: int, num_std: float) -> tuple[pd.Series, pd.Series, pd.Series]:
    require_window("period", period, 1)
    upper, middle, lower = kernels.bollinger_bands(as_float64(close), period, float(num_std))
    return (
        to_series(upper, close.index, close.name),
        to_series(middle, close.index, close.name),
        to_series(lower, close.index, close.name),
    )


def compute_macd(
    close: pd.Series, fast_period: int, slow_period: int, signal_period: int
) -> tuple[pd.Series, pd.Series, pd.Series]:
    require_window("fast_period", fast_period, 1)
    require_window("slow_period", slow_period, 1)
    require_window("signal_period", signal_period, 1)
    line, signal, histogram = kernels.macd(as_float64(close), fast_period, slow_period, signal_period)
    return (
        to_series(line, close.index, close.name),
        to_series(signal, close.index, close.name),
        to_series(histogram, close.index, close.name),
    )


def compute_donchian_channels(high: pd.Series, low: pd.Series, period: int) -> tuple[pd.Series, pd.Series]:
    require_window("period", period, 1)
    idx = shared_index(high, low)
    upper, lower = kernels.donchian_channels(as_float64(high), as_float64(low), period)
    return (
        to_series(upper, idx, high.name),
        to_series(lower, idx, low.name),
    )


def compute_atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int) -> pd.Series:
    """
    Computes Wilder's Average True Range (ATR) using Wilder's smoothing/exponential moving average.
    """
    require_window("period", period, 1)
    idx = shared_index(close, high, low)
    res = kernels.atr(as_float64(high), as_float64(low), as_float64(close), period)
    return to_series(res, idx, None)
