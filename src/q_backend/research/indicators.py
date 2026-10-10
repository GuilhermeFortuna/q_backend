"""Supported indicator helpers for research and feature engineering.

Wraps existing q_backend indicator implementations and ensures proper
input validation, exact index preservation, and clear errors.
"""

from __future__ import annotations

import math
from typing import Sequence
import numpy as np
import pandas as pd

from q_backend.backtesting.moving_averages import VALID_MA_TYPES, compute_ma
from q_backend.backtesting.technical_indicators import (
    compute_atr,
    compute_bollinger_bands,
    compute_donchian_channels,
    compute_macd,
    compute_realized_vol,
    compute_rsi,
    compute_yang_zhang,
)

__all__ = (
    "ma",
    "rsi",
    "atr",
    "bollinger",
    "macd",
    "donchian",
    "realized_vol",
    "yang_zhang",
)


def _validate_series(series: object, name: str) -> pd.Series:
    if not isinstance(series, pd.Series):
        raise TypeError(f"'{name}' must be a pandas Series, got {type(series).__name__}")
    if not pd.api.types.is_numeric_dtype(series.dtype):
        raise TypeError(f"'{name}' must have a numeric dtype, got {series.dtype}")
    # Reject infinities
    if np.isinf(series.to_numpy(dtype=np.float64, na_value=np.nan)).any():
        raise ValueError(f"'{name}' contains infinite values, which are not permitted")
    return series


def _validate_frame(frame: object, name: str, required_cols: Sequence[str]) -> pd.DataFrame:
    if not isinstance(frame, pd.DataFrame):
        raise TypeError(f"'{name}' must be a pandas DataFrame, got {type(frame).__name__}")
    missing = [c for c in required_cols if c not in frame.columns]
    if missing:
        raise ValueError(f"'{name}' is missing required columns: {missing}")
    for col in required_cols:
        s = frame[col]
        if not pd.api.types.is_numeric_dtype(s.dtype):
            raise TypeError(f"Column '{col}' in '{name}' must have a numeric dtype, got {s.dtype}")
        if np.isinf(s.to_numpy(dtype=np.float64, na_value=np.nan)).any():
            raise ValueError(f"Column '{col}' in '{name}' contains infinite values, which are not permitted")
    return frame


def _validate_window(value: object, name: str, min_val: int = 1) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, np.integer)):
        raise TypeError(f"'{name}' must be an integer, got {type(value).__name__}")
    int_val = int(value)
    if int_val < min_val:
        raise ValueError(f"'{name}' must be an integer >= {min_val}, got {int_val}")
    return int_val


def _validate_positive_float(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, np.floating, np.integer)):
        raise TypeError(f"'{name}' must be a number, got {type(value).__name__}")
    float_val = float(value)
    if not math.isfinite(float_val) or float_val <= 0:
        raise ValueError(f"'{name}' must be a finite number > 0, got {value}")
    return float_val


def ma(close: pd.Series, period: int, kind: str = "sma") -> pd.Series:
    """
    Compute moving average on close prices.

    Supported kinds (case-insensitive): ``'sma'`` (Simple), ``'ema'`` (Exponential),
    ``'wma'`` (Weighted), ``'smma'`` (Smoothed), and ``'hma'`` (Hull).

    Parameters
    ----------
    close : pandas.Series
        Numeric price series.
    period : int
        Calculation window period. Must be an integer >= 1.
    kind : str, default 'sma'
        Type of moving average to compute.

    Returns
    -------
    pandas.Series
        Moving average series sharing the exact index and name of ``close``.
        Warm-up periods contain NaN.

    Raises
    ------
    TypeError
        If ``close`` is not a Series or ``period`` is not an integer.
    ValueError
        If ``kind`` is unrecognized or ``close`` contains infinities.
    """
    s = _validate_series(close, "close")
    p = _validate_window(period, "period", min_val=1)
    if not isinstance(kind, str):
        raise TypeError(f"'kind' must be a string, got {type(kind).__name__}")
    k = kind.lower().strip()
    if k not in VALID_MA_TYPES:
        allowed = ", ".join(sorted(VALID_MA_TYPES))
        raise ValueError(f"Invalid MA kind '{kind}'. Must be one of: {allowed}")

    if s.empty:
        return pd.Series(dtype=np.float64, index=s.index, name=s.name)

    return compute_ma(s, period=p, ma_type=k)


def rsi(close: pd.Series, period: int) -> pd.Series:
    """
    Compute Wilder's Relative Strength Index (RSI).

    Measures momentum by evaluating the magnitude of recent price gains versus
    losses using Wilder's smoothed moving average (RMA).

    Parameters
    ----------
    close : pandas.Series
        Numeric price series.
    period : int
        RSI lookback window. Must be an integer >= 1.

    Returns
    -------
    pandas.Series
        Series with values between 0.0 and 100.0, indexed identically to ``close``.
        The initial ``period`` bars contain NaN during warm-up.

    Raises
    ------
    TypeError
        If ``close`` is not a pandas Series or ``period`` is not an integer.
    ValueError
        If ``period < 1`` or ``close`` contains non-finite values.
    """
    s = _validate_series(close, "close")
    p = _validate_window(period, "period", min_val=1)
    if s.empty:
        return pd.Series(dtype=np.float64, index=s.index, name=s.name)
    return compute_rsi(s, period=p)


def atr(frame: pd.DataFrame, period: int) -> pd.Series:
    """
    Compute Wilder's Average True Range (ATR) on OHLC bars.

    Parameters
    ----------
    frame : pandas.DataFrame
        DataFrame containing ``'high'``, ``'low'``, and ``'close'`` columns.
    period : int
        Smoothing period. Must be an integer >= 1.

    Returns
    -------
    pandas.Series
        Series of ATR values sharing the exact index of ``frame``. Initial
        ``period`` bars contain NaN during warm-up.

    Raises
    ------
    ValueError
        If ``frame`` is missing required columns or ``period < 1``.
    """
    df = _validate_frame(frame, "frame", ("high", "low", "close"))
    p = _validate_window(period, "period", min_val=1)
    if df.empty:
        return pd.Series(dtype=np.float64, index=df.index, name=None)
    res = compute_atr(high=df["high"], low=df["low"], close=df["close"], period=p)
    res.index = df.index
    return res


def bollinger(close: pd.Series, period: int, num_std: float = 2.0) -> tuple[pd.Series, pd.Series, pd.Series]:
    """
    Compute Bollinger Bands returning (upper, middle, lower).

    Parameters
    ----------
    close : pandas.Series
        Numeric price series.
    period : int
        Moving average window. Must be an integer >= 1.
    num_std : float, default 2.0
        Standard deviation multiplier for bands. Must be finite and > 0.

    Returns
    -------
    tuple of pandas.Series
        A 3-tuple of ``(upper_band, middle_band, lower_band)`` sharing the index of ``close``.
    """
    s = _validate_series(close, "close")
    p = _validate_window(period, "period", min_val=1)
    ns = _validate_positive_float(num_std, "num_std")
    if s.empty:
        empty_s = pd.Series(dtype=np.float64, index=s.index, name=s.name)
        return empty_s, empty_s.copy(), empty_s.copy()
    return compute_bollinger_bands(s, period=p, num_std=ns)


def macd(
    close: pd.Series,
    fast_period: int = 12,
    slow_period: int = 26,
    signal_period: int = 9,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """
    Compute Moving Average Convergence Divergence (MACD).

    Parameters
    ----------
    close : pandas.Series
        Numeric price series.
    fast_period : int, default 12
        Fast EMA lookback period (>= 1).
    slow_period : int, default 26
        Slow EMA lookback period (>= 1).
    signal_period : int, default 9
        Signal line EMA lookback period (>= 1).

    Returns
    -------
    tuple of pandas.Series
        A 3-tuple of ``(macd_line, signal_line, histogram)`` sharing the index of ``close``.
    """
    s = _validate_series(close, "close")
    fast = _validate_window(fast_period, "fast_period", min_val=1)
    slow = _validate_window(slow_period, "slow_period", min_val=1)
    sig = _validate_window(signal_period, "signal_period", min_val=1)
    if s.empty:
        empty_s = pd.Series(dtype=np.float64, index=s.index, name=s.name)
        return empty_s, empty_s.copy(), empty_s.copy()
    return compute_macd(
        close=s,
        fast_period=fast,
        slow_period=slow,
        signal_period=sig,
    )


def donchian(frame: pd.DataFrame, period: int) -> tuple[pd.Series, pd.Series]:
    """
    Compute Donchian Channels returning (upper, lower).

    Parameters
    ----------
    frame : pandas.DataFrame
        DataFrame containing ``'high'`` and ``'low'`` columns.
    period : int
        Lookback window. Must be an integer >= 1.

    Returns
    -------
    tuple of pandas.Series
        A 2-tuple of ``(upper_channel, lower_channel)`` sharing the index of ``frame``.
    """
    df = _validate_frame(frame, "frame", ("high", "low"))
    p = _validate_window(period, "period", min_val=1)
    if df.empty:
        empty_high = pd.Series(dtype=np.float64, index=df.index, name=df["high"].name)
        empty_low = pd.Series(dtype=np.float64, index=df.index, name=df["low"].name)
        return empty_high, empty_low
    upper, lower = compute_donchian_channels(high=df["high"], low=df["low"], period=p)
    upper.index = df.index
    lower.index = df.index
    return upper, lower


def realized_vol(close: pd.Series, window: int, periods_per_year: int = 252) -> pd.Series:
    """
    Compute rolling annualized realized volatility from close log returns.

    Parameters
    ----------
    close : pandas.Series
        Numeric price series.
    window : int
        Rolling window in bars. Must be an integer >= 1.
    periods_per_year : int, default 252
        Annualization multiplier (e.g., 252 for daily bars).

    Returns
    -------
    pandas.Series
        Annualized volatility series sharing the index of ``close``.
    """
    s = _validate_series(close, "close")
    w = _validate_window(window, "window", min_val=1)
    ppy = _validate_window(periods_per_year, "periods_per_year", min_val=1)
    if s.empty:
        return pd.Series(dtype=np.float64, index=s.index, name=s.name)
    return compute_realized_vol(s, window=w, periods_per_year=ppy)


def yang_zhang(frame: pd.DataFrame, window: int, periods_per_year: int = 252) -> pd.Series:
    """
    Compute rolling annualized Yang-Zhang (2000) historical volatility.

    Parameters
    ----------
    frame : pandas.DataFrame
        DataFrame containing ``'open'``, ``'high'``, ``'low'``, and ``'close'`` columns.
    window : int
        Rolling calculation window. Must be an integer >= 2.
    periods_per_year : int, default 252
        Annualization factor (e.g., 252 for daily bars).

    Returns
    -------
    pandas.Series
        Annualized volatility series sharing the exact index of ``frame``.

    Notes
    -----
    Yang-Zhang is an OHLC estimator with minimum variance among drift-independent
    continuous and jump volatility estimators. It combines Rogers-Satchell volatility,
    open jump volatility, and continuous close-to-open volatility.
    """
    df = _validate_frame(frame, "frame", ("open", "high", "low", "close"))
    w = _validate_window(window, "window", min_val=2)
    ppy = _validate_window(periods_per_year, "periods_per_year", min_val=1)
    if df.empty:
        return pd.Series(dtype=np.float64, index=df.index, name=None)
    res = compute_yang_zhang(
        open_=df["open"],
        high=df["high"],
        low=df["low"],
        close=df["close"],
        window=w,
        periods_per_year=ppy,
    )
    res.index = df.index
    return res
