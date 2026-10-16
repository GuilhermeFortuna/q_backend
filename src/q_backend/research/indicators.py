"""Supported indicator helpers for research and feature engineering.

Wraps existing q_backend calculations with input validation, index preservation,
and clear errors. Bar helpers are pure; liquidity helpers read an explicit local
TickStore without fetching or synchronizing history.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Sequence
import numpy as np
import pandas as pd

from q_backend.backtesting.moving_averages import VALID_MA_TYPES, compute_ma
from q_backend.backtesting.technical_indicators import (
    compute_atr,
    compute_atr_fraction,
    compute_ewma_trend_spread,
    compute_ewma_vol_per_bar,
    compute_log_return,
    compute_normalized_ewma_trend_spread,
    compute_normalized_momentum,
    compute_per_bar_volatility,
    compute_price_zscore_log,
    compute_realized_vol_per_bar,
    compute_rogers_satchell,
    compute_true_range,
    compute_volatility_log_ratio,
    compute_bollinger_bands,
    compute_donchian_channels,
    compute_macd,
    compute_realized_vol,
    compute_rsi,
    compute_yang_zhang,
)

from q_backend.backtesting.transforms import compute_pct_change, compute_rolling_rank, compute_rolling_zscore
from q_backend.market_data.volume_at_price import VolumeProfile
from q_backend.research.swings import LiveSwingDetector

if TYPE_CHECKING:
    from q_backend.research.tick_store import TickStore

__all__ = (
    "ma",
    "rsi",
    "atr",
    "bollinger",
    "macd",
    "donchian",
    "realized_vol",
    "yang_zhang",
    "volume_ma",
    "rolling_zscore",
    "rolling_rank",
    "pct_change",
    "log_return",
    "normalized_momentum",
    "normalized_ewma_trend_spread",
    "price_zscore_log",
    "ewma_trend_spread",
    "atr_fraction",
    "true_range",
    "per_bar_realized_vol",
    "per_bar_ewma_vol",
    "rogers_satchell",
    "per_bar_yang_zhang",
    "volatility_log_ratio",
    "quote_spread",
    "spread_vs_volatility",
    "VolumeProfile",
    "volume_at_price",
    "value_area_levels",
    "top_bottom",
)


def _validate_series(series: object, name: str) -> pd.Series:
    if not isinstance(series, pd.Series):
        raise TypeError(f"'{name}' must be a pandas Series, got {type(series).__name__}")
    if not pd.api.types.is_numeric_dtype(series.dtype):
        raise TypeError(f"'{name}' must have a numeric dtype, got {series.dtype}")
    if pd.api.types.is_complex_dtype(series.dtype):
        raise TypeError(f"'{name}' contains complex values; real numeric data is required")
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
        if pd.api.types.is_complex_dtype(s.dtype):
            raise TypeError(f"Column '{col}' in '{name}' contains complex values; real numeric data is required")
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


def volume_ma(volume: pd.Series, period: int) -> pd.Series:
    """Compute a simple moving average of bar volume.

    Parameters
    ----------
    volume : pandas.Series
        Numeric bar volume; units depend on the data source.
    period : int
        Trailing bar count, at least 1.

    Returns
    -------
    pandas.Series
        Mean volume, with the input's exact index and name. Incomplete windows are NaN.
    """
    return ma(volume, period=period, kind="sma")


def rolling_zscore(close: pd.Series, window: int) -> pd.Series:
    """Compute the closing price's trailing sample z-score.

    Parameters
    ----------
    close : pandas.Series
        Numeric closing prices.
    window : int
        Trailing bar count, at least 1.

    Returns
    -------
    pandas.Series
        Dimensionless distance from the rolling mean, preserving index and name.
        Incomplete, constant, and one-bar windows have undefined deviation and return NaN.

    Notes
    -----
    Uses sample standard deviation (ddof=1), including the current close.
    """
    s = _validate_series(close, "close")
    w = _validate_window(window, "window")
    return compute_rolling_zscore(s, w)


def rolling_rank(close: pd.Series, window: int) -> pd.Series:
    """Compute the current close's percentile rank within its trailing window.

    Parameters
    ----------
    close : pandas.Series
        Numeric closing prices.
    window : int
        Trailing bar count, at least 1.

    Returns
    -------
    pandas.Series
        Fractions on the input's exact index; incomplete or missing windows are NaN.
        The result has no Series name, matching the backend transform.

    Notes
    -----
    Rank is count(window values <= current close) / window. Ties count fully;
    a constant window ranks 1, and the minimum possible rank is 1/window.
    """
    s = _validate_series(close, "close")
    w = _validate_window(window, "window")
    return compute_rolling_rank(s, w)


def pct_change(close: pd.Series, change_bars: int = 1) -> pd.Series:
    """Compute a simple return over a positive bar lookback.

    Parameters
    ----------
    close : pandas.Series
        Numeric closing prices.
    change_bars : int, default 1
        Number of bars to look back, at least 1.

    Returns
    -------
    pandas.Series
        Decimal returns (0.01 means 1%), preserving index and name. Initial
        lookback bars are NaN. Division by zero may produce infinity or NaN.
    """
    s = _validate_series(close, "close")
    lag = _validate_window(change_bars, "change_bars")
    return compute_pct_change(s, lag)


def log_return(close: pd.Series, horizon: int = 1) -> pd.Series:
    """Compute ln(current close) minus ln(the earlier close).

    Parameters
    ----------
    close : pandas.Series
        Closing prices. Non-positive or missing prices produce NaN.
    horizon : int, default 1
        Return lookback in bars, at least 1.

    Returns
    -------
    pandas.Series
        Log returns, preserving index and name. The initial horizon bars are NaN.
    """
    s = _validate_series(close, "close")
    h = _validate_window(horizon, "horizon")
    return compute_log_return(s, h)


def price_zscore_log(close: pd.Series, window: int) -> pd.Series:
    """Compute a sample z-score of log closing prices.

    Parameters
    ----------
    close : pandas.Series
        Closing prices; non-positive prices become missing values.
    window : int
        Trailing bar count, at least 2.

    Returns
    -------
    pandas.Series
        Dimensionless log-price z-score, preserving index and name. Incomplete
        or constant windows are NaN; uses sample standard deviation (ddof=1).
    """
    s = _validate_series(close, "close")
    w = _validate_window(window, "window", min_val=2)
    return compute_price_zscore_log(s, w)


def _ordered_half_lives(fast: object, slow: object, fast_name: str, slow_name: str) -> tuple[float, float]:
    fast_value = _validate_positive_float(fast, fast_name)
    slow_value = _validate_positive_float(slow, slow_name)
    if fast_value >= slow_value:
        raise ValueError(f"'{fast_name}' must be smaller than '{slow_name}'")
    return fast_value, slow_value


def ewma_trend_spread(close: pd.Series, *, fast_half_life: float = 8.0, slow_half_life: float = 32.0) -> pd.Series:
    """Compute the fast-minus-slow EWMA spread of log closing prices.

    Parameters
    ----------
    close : pandas.Series
        Closing prices; non-positive prices become missing values.
    fast_half_life : float, default 8.0
        Fast EWMA half-life in bars, finite and positive.
    slow_half_life : float, default 32.0
        Slow EWMA half-life in bars, greater than the fast half-life.

    Returns
    -------
    pandas.Series
        Log-price trend spread, preserving index and name. Initial values are
        NaN until the backend's half-life warm-up is complete.
    """
    s = _validate_series(close, "close")
    fast, slow = _ordered_half_lives(fast_half_life, slow_half_life, "fast_half_life", "slow_half_life")
    return compute_ewma_trend_spread(s, fast, slow)


def atr_fraction(frame: pd.DataFrame, period: int) -> pd.Series:
    """Compute Wilder ATR divided by the current close.

    Parameters
    ----------
    frame : pandas.DataFrame
        Bars with numeric high, low, and close columns.
    period : int
        Wilder smoothing period, at least 1.

    Returns
    -------
    pandas.Series
        Dimensionless range fraction (0.01 means 1%), on the exact frame index.
        Warm-up and invalid denominators produce NaN; the result has no name.
    """
    df = _validate_frame(frame, "frame", ("high", "low", "close"))
    p = _validate_window(period, "period")
    result = compute_atr_fraction(df["high"], df["low"], df["close"], p)
    result.index = df.index
    return result


def true_range(frame: pd.DataFrame) -> pd.Series:
    """Compute each bar's range, including its gap from the preceding close.

    Parameters
    ----------
    frame : pandas.DataFrame
        Bars with numeric high, low, and close columns.

    Returns
    -------
    pandas.Series
        True range in price units on the exact frame index, with no name.
        The first bar is NaN because there is no previous close.
    """
    df = _validate_frame(frame, "frame", ("high", "low", "close"))
    result = compute_true_range(df["high"], df["low"], df["close"])
    result.index = df.index
    return result


def per_bar_realized_vol(close: pd.Series, window: int) -> pd.Series:
    """Compute root-mean-square one-bar log returns without annualization.

    Parameters
    ----------
    close : pandas.Series
        Closing prices; non-positive prices become missing values.
    window : int
        Number of trailing one-bar log returns, at least 1.

    Returns
    -------
    pandas.Series
        Per-bar log-return scale, preserving index and name. Incomplete windows
        are NaN. This RMS estimator does not subtract the mean return.
    """
    s = _validate_series(close, "close")
    w = _validate_window(window, "window")
    return compute_realized_vol_per_bar(s, w)


def per_bar_ewma_vol(close: pd.Series, half_life: float = 32.0) -> pd.Series:
    """Compute the square root of an EWMA of squared one-bar log returns.

    Parameters
    ----------
    close : pandas.Series
        Closing prices; non-positive prices become missing values.
    half_life : float, default 32.0
        Decay half-life in bars, finite and positive.

    Returns
    -------
    pandas.Series
        Per-bar, unannualized volatility, preserving index and name. Initial
        values are NaN until the backend's half-life warm-up is complete.
    """
    s = _validate_series(close, "close")
    hl = _validate_positive_float(half_life, "half_life")
    return compute_ewma_vol_per_bar(s, hl)


def rogers_satchell(frame: pd.DataFrame, window: int) -> pd.Series:
    """Compute per-bar Rogers-Satchell OHLC volatility.

    Parameters
    ----------
    frame : pandas.DataFrame
        Bars with numeric open, high, low, and close columns.
    window : int
        Trailing bar count, at least 1.

    Returns
    -------
    pandas.Series
        Unannualized range volatility on the exact frame index, with no name.
        Incomplete windows and invalid prices are NaN. Opening gaps are not included.
    """
    df = _validate_frame(frame, "frame", ("open", "high", "low", "close"))
    w = _validate_window(window, "window")
    result = compute_rogers_satchell(df["open"], df["high"], df["low"], df["close"], w)
    result.index = df.index
    return result


def per_bar_yang_zhang(frame: pd.DataFrame, window: int) -> pd.Series:
    """Compute Yang-Zhang OHLC volatility without annualization.

    Parameters
    ----------
    frame : pandas.DataFrame
        Bars with numeric open, high, low, and close columns.
    window : int
        Trailing bar count, at least 2.

    Returns
    -------
    pandas.Series
        Per-bar volatility on the exact frame index, including opening gaps.
        Equivalent to yang_zhang(frame, window, periods_per_year=1).
    """
    return yang_zhang(frame, window=window, periods_per_year=1)


def _volatility_parameters(
    estimator: object,
    window: object,
    half_life: object,
    *,
    window_name: str = "vol_window",
    half_life_name: str = "vol_half_life",
) -> tuple[str, int | None, float | None]:
    if not isinstance(estimator, str):
        raise TypeError("'vol_estimator' must be a string")
    normalized = estimator.strip().lower()
    allowed = ("realized", "ewma", "rogers_satchell", "yang_zhang")
    if normalized not in allowed:
        raise ValueError(f"'vol_estimator' must be one of {', '.join(allowed)}")
    if normalized == "ewma":
        return normalized, None, _validate_positive_float(half_life, half_life_name)
    minimum = 2 if normalized == "yang_zhang" else 1
    return normalized, _validate_window(window, window_name, min_val=minimum), None


def _validate_vol_lag(value: object) -> int:
    lag = _validate_window(value, "vol_lag", min_val=0)
    if lag > 1:
        raise ValueError("'vol_lag' must be 0 or 1")
    return lag


def normalized_momentum(
    frame: pd.DataFrame,
    *,
    horizon: int = 20,
    vol_estimator: str = "realized",
    vol_window: int | None = 20,
    vol_half_life: float | None = 32.0,
    vol_lag: int = 1,
) -> pd.Series:
    """Scale a horizon log return by per-bar volatility and sqrt(horizon).

    Parameters
    ----------
    frame : pandas.DataFrame
        Bars with numeric open, high, low, and close columns.
    horizon : int, default 20
        Return lookback in bars, at least 1.
    vol_estimator : str, default 'realized'
        Case-insensitive realized, ewma, rogers_satchell, or yang_zhang.
    vol_window : int or None, default 20
        Volatility window, at least 1 (2 for Yang-Zhang); ignored for EWMA.
    vol_half_life : float or None, default 32.0
        Positive volatility half-life in bars; used only for EWMA.
    vol_lag : int, default 1
        Use previous-bar volatility (1) or current-bar volatility (0).

    Returns
    -------
    pandas.Series
        Dimensionless momentum on the exact frame index, named close. Warm-up,
        missing prices, and volatility at or below the kernel floor produce NaN.
    """
    df = _validate_frame(frame, "frame", ("open", "high", "low", "close"))
    h = _validate_window(horizon, "horizon")
    estimator, window, half_life = _volatility_parameters(vol_estimator, vol_window, vol_half_life)
    lag = _validate_vol_lag(vol_lag)
    result = compute_normalized_momentum(
        df["close"],
        df["open"],
        df["high"],
        df["low"],
        horizon=h,
        vol_estimator=estimator,
        vol_window=window,
        vol_half_life=half_life,
        vol_lag=lag,
    )
    result.index = df.index
    return result


def normalized_ewma_trend_spread(
    frame: pd.DataFrame,
    *,
    fast_half_life: float = 8.0,
    slow_half_life: float = 32.0,
    vol_estimator: str = "realized",
    vol_window: int | None = 20,
    vol_half_life: float | None = 32.0,
    vol_lag: int = 1,
) -> pd.Series:
    """Scale the fast-minus-slow log-price EWMA spread by per-bar volatility.

    Parameters
    ----------
    frame : pandas.DataFrame
        Bars with numeric open, high, low, and close columns.
    fast_half_life : float, default 8.0
        Fast log-price half-life in bars, finite and positive.
    slow_half_life : float, default 32.0
        Slow log-price half-life, greater than the fast half-life.
    vol_estimator : str, default 'realized'
        Case-insensitive realized, ewma, rogers_satchell, or yang_zhang.
    vol_window : int or None, default 20
        Volatility window, at least 1 (2 for Yang-Zhang); ignored for EWMA.
    vol_half_life : float or None, default 32.0
        Positive volatility half-life; used only for EWMA.
    vol_lag : int, default 1
        Use previous-bar volatility (1) or current-bar volatility (0).

    Returns
    -------
    pandas.Series
        Dimensionless trend spread on the exact frame index, named close.
        Warm-up, missing inputs, and near-zero volatility produce NaN.
    """
    df = _validate_frame(frame, "frame", ("open", "high", "low", "close"))
    fast, slow = _ordered_half_lives(fast_half_life, slow_half_life, "fast_half_life", "slow_half_life")
    estimator, window, half_life = _volatility_parameters(vol_estimator, vol_window, vol_half_life)
    lag = _validate_vol_lag(vol_lag)
    result = compute_normalized_ewma_trend_spread(
        df["close"],
        df["open"],
        df["high"],
        df["low"],
        fast_half_life=fast,
        slow_half_life=slow,
        vol_estimator=estimator,
        vol_window=window,
        vol_half_life=half_life,
        vol_lag=lag,
    )
    result.index = df.index
    return result


def volatility_log_ratio(
    frame: pd.DataFrame,
    *,
    vol_estimator: str = "realized",
    short_window: int | None = 10,
    long_window: int | None = 40,
    short_half_life: float | None = 8.0,
    long_half_life: float | None = 32.0,
) -> pd.Series:
    """Compute ln(short volatility) minus ln(long volatility).

    Parameters
    ----------
    frame : pandas.DataFrame
        Bars with numeric open, high, low, and close columns.
    vol_estimator : str, default 'realized'
        Case-insensitive realized, ewma, rogers_satchell, or yang_zhang.
    short_window : int or None, default 10
        Short trailing window; at least 1 (2 for Yang-Zhang), ignored for EWMA.
    long_window : int or None, default 40
        Long window, greater than the short window; ignored for EWMA.
    short_half_life : float or None, default 8.0
        Positive short volatility half-life; used only for EWMA.
    long_half_life : float or None, default 32.0
        Long half-life, greater than the short half-life; used only for EWMA.

    Returns
    -------
    pandas.Series
        Dimensionless log ratio on the exact frame index, with no name.
        Zero means equal volatility; missing or near-zero estimates produce NaN.
    """
    df = _validate_frame(frame, "frame", ("open", "high", "low", "close"))
    estimator, short_w, short_hl = _volatility_parameters(
        vol_estimator, short_window, short_half_life, window_name="short_window", half_life_name="short_half_life"
    )
    _, long_w, long_hl = _volatility_parameters(
        vol_estimator, long_window, long_half_life, window_name="long_window", half_life_name="long_half_life"
    )
    if estimator == "ewma":
        short_hl, long_hl = _ordered_half_lives(short_hl, long_hl, "short_half_life", "long_half_life")
    elif short_w >= long_w:
        raise ValueError("'short_window' must be smaller than 'long_window'")
    args = (df["open"], df["high"], df["low"], df["close"])
    short = compute_per_bar_volatility(*args, estimator=estimator, window=short_w, half_life=short_hl)
    long = compute_per_bar_volatility(*args, estimator=estimator, window=long_w, half_life=long_hl)
    result = compute_volatility_log_ratio(short, long)
    result.index = df.index
    return result


def _read_relative_spread(
    frame: pd.DataFrame,
    ticks: TickStore,
    timeframe: str,
    output: str,
    max_quote_age_ms: int,
    min_coverage: float,
) -> tuple[pd.DataFrame, np.ndarray, str | None]:
    # Lazy imports keep pure bar calculations independent of tick-store dependencies.
    from q_backend.market_data.exogenous_context import bar_duration
    from q_backend.market_data.quote_spread import bar_bounds_msc, quote_spread as aggregate_quotes
    from q_backend.research.tick_store import TickStore

    df = _validate_frame(frame, "frame", ())
    if not isinstance(ticks, TickStore):
        raise TypeError("'ticks' must be a TickStore")
    if not isinstance(df.index, pd.DatetimeIndex):
        raise TypeError("'frame' must have a DatetimeIndex of bar-open timestamps")
    if df.index.hasnans or not df.index.is_unique or not df.index.is_monotonic_increasing:
        raise ValueError("'frame' index must contain unique, chronological bar timestamps without NaT")
    if not isinstance(timeframe, str):
        raise TypeError("'timeframe' must be a string")
    tf = timeframe.strip().upper()
    bar_duration(tf)  # Validate even on empty frames; use the shared service's supported intervals.
    opens, closes = bar_bounds_msc(df.index, tf)
    if np.any(opens[1:] < closes[:-1]):
        raise ValueError("'frame' bar intervals must not overlap for the selected timeframe")
    if not isinstance(output, str) or output not in ("last", "time_weighted_mean", "max"):
        raise ValueError("'output' must be last, time_weighted_mean, or max")
    age = _validate_window(max_quote_age_ms, "max_quote_age_ms", min_val=0)
    if isinstance(min_coverage, bool) or not isinstance(min_coverage, (int, float, np.integer, np.floating)):
        raise TypeError("'min_coverage' must be a number")
    coverage = float(min_coverage)
    if not math.isfinite(coverage) or not 0 <= coverage <= 1:
        raise ValueError("'min_coverage' must be finite and between 0 and 1")
    metadata = df.attrs.get("q_research", {})
    if not isinstance(metadata, dict):
        raise TypeError("'frame' q_research metadata must be a dictionary")
    if metadata.get("symbol", ticks.symbol) != ticks.symbol:
        raise ValueError("'frame' symbol metadata conflicts with ticks.symbol")
    if "timeframe" in metadata and metadata["timeframe"] != tf:
        raise ValueError("'frame' timeframe metadata conflicts with timeframe")
    quotes = aggregate_quotes(ticks, ticks.symbol, df.index, tf, max_quote_age_ms=age)
    return df, quotes.select(output, coverage), quotes.unavailable_reason


def quote_spread(
    frame: pd.DataFrame,
    *,
    ticks: TickStore,
    timeframe: str,
    output: str = "time_weighted_mean",
    max_quote_age_ms: int = 60_000,
    min_coverage: float = 0.0,
) -> pd.Series:
    """Read stored bid-ask quotes and aggregate spread to bars in basis points.

    Parameters
    ----------
    frame : pandas.DataFrame
        A unique, chronological DatetimeIndex of non-overlapping bar opens; price columns are
        not required. Naive timestamps mean Brasilia local time. Any q_research
        symbol/timeframe metadata must match the explicit arguments.
    ticks : q_backend.research.TickStore
        Explicit local store; its symbol identifies the instrument. Never synced by this helper.
    timeframe : str
        Fixed bar interval supported by the shared quote service: M1, M5, M10,
        M15, H1, H2, H4, or D1 (case-insensitive).
    output : str, default 'time_weighted_mean'
        Spread aggregation: time_weighted_mean, last (quote valid at bar close), or max.
    max_quote_age_ms : int, default 60000
        Nonnegative milliseconds a quote remains valid without a new update.
    min_coverage : float, default 0.0
        Required fraction of each bar covered by valid quotes, between 0 and 1.

    Returns
    -------
    pandas.Series
        Quoted spread in basis points (1 bp = 0.01%) on the exact frame index,
        with no name. Missing/stale/under-covered quotes are NaN. The attrs key
        unavailable_reason contains the service explanation, or None.

    Notes
    -----
    Reads existing local files only: no gateway, synchronization, database, or
    Redis. Quotes never carry across sessions. No automatic history is loaded.
    """
    df, relative, reason = _read_relative_spread(frame, ticks, timeframe, output, max_quote_age_ms, min_coverage)
    result = pd.Series(relative * 10_000.0, index=df.index)
    result.attrs["unavailable_reason"] = reason
    return result


def spread_vs_volatility(
    frame: pd.DataFrame,
    *,
    ticks: TickStore,
    timeframe: str,
    spread_output: str = "time_weighted_mean",
    max_quote_age_ms: int = 60_000,
    min_coverage: float = 0.0,
    vol_estimator: str = "realized",
    vol_window: int | None = 20,
    vol_half_life: float | None = 32.0,
    vol_lag: int = 1,
) -> pd.Series:
    """Divide relative quoted spread by lagged per-bar volatility.

    Parameters
    ----------
    frame : pandas.DataFrame
        Numeric open, high, low, and close on a unique, chronological DatetimeIndex
        of non-overlapping bars.
        Naive timestamps mean Brasilia local time; metadata must match the arguments.
    ticks : q_backend.research.TickStore
        Explicit local store for the instrument; never synchronized by this helper.
    timeframe : str
        Fixed quote-bar interval: M1, M5, M10, M15, H1, H2, H4, or D1.
    spread_output : str, default 'time_weighted_mean'
        Spread aggregation: time_weighted_mean, last, or max.
    max_quote_age_ms : int, default 60000
        Nonnegative quote lifetime in milliseconds.
    min_coverage : float, default 0.0
        Minimum covered fraction of a bar, between 0 and 1.
    vol_estimator : str, default 'realized'
        Case-insensitive realized, ewma, rogers_satchell, or yang_zhang.
    vol_window : int or None, default 20
        Volatility window, at least 1 (2 for Yang-Zhang); ignored for EWMA.
    vol_half_life : float or None, default 32.0
        Positive half-life in bars; used only for EWMA.
    vol_lag : int, default 1
        Use previous-bar volatility (1) or current-bar volatility (0).

    Returns
    -------
    pandas.Series
        Dimensionless spread/volatility ratio on the exact frame index, with no
        name. Missing quotes, warm-up, and near-zero volatility yield NaN.
        The attrs key unavailable_reason contains the quote service explanation, or None.

    Notes
    -----
    The numerator is relative spread, not basis points. This helper reads local
    quote files without syncing or fetching. Supply enough bars for volatility warm-up.
    """
    from q_backend.market_data.quote_spread import spread_vs_volatility as normalize_spread

    df = _validate_frame(frame, "frame", ("open", "high", "low", "close"))
    estimator, window, half_life = _volatility_parameters(vol_estimator, vol_window, vol_half_life)
    lag = _validate_vol_lag(vol_lag)
    _, relative, reason = _read_relative_spread(df, ticks, timeframe, spread_output, max_quote_age_ms, min_coverage)
    result = normalize_spread(
        df,
        relative,
        vol_estimator=estimator,
        vol_window=window if window is not None else 20,
        vol_half_life=half_life if half_life is not None else 32.0,
        vol_lag=lag,
    )
    result.index = df.index
    result.attrs["unavailable_reason"] = reason
    return result


def volume_at_price(
    ticks: TickStore,
    *,
    start: str,
    end: str,
    tick_size: float,
    by: str = "session",
    timeframe: str | None = None,
    row_ticks: int = 1,
    value_area: float = 0.7,
) -> VolumeProfile:
    """Build volume-at-price profiles from stored trades in ``ticks``."""
    from q_backend.market_data.volume_at_price import build_volume_profile
    from q_backend.research.tick_store import TickStore

    if not isinstance(ticks, TickStore):
        raise TypeError("'ticks' must be a TickStore")
    profile = build_volume_profile(
        ticks,
        ticks.symbol,
        start=start,
        end=end,
        tick_size=tick_size,
        by=by,  # type: ignore[arg-type]
        timeframe=timeframe,
        row_ticks=row_ticks,
        value_area=value_area,
    )
    return profile


def value_area_levels(
    frame: pd.DataFrame,
    *,
    ticks: TickStore,
    timeframe: str,
    mode: str = "developing",
    tick_size: float | None = None,
    row_ticks: int = 1,
    value_area: float = 0.7,
) -> pd.DataFrame:
    """Return POC, VAH and VAL on ``frame``'s index from stored trades."""
    from q_backend.market_data.volume_at_price import value_area_levels_from_ticks
    from q_backend.research.tick_store import TickStore

    df = _validate_frame(frame, "frame", ())
    if not isinstance(ticks, TickStore):
        raise TypeError("'ticks' must be a TickStore")
    resolved_tick = tick_size
    if resolved_tick is None:
        metadata = df.attrs.get("q_research", {})
        if isinstance(metadata, dict):
            raw = metadata.get("tick_size")
            if raw is not None:
                resolved_tick = float(raw)
    if resolved_tick is None:
        raise ValueError(
            "tick_size is required when frame.attrs['q_research']['tick_size'] is not set; "
            "pass tick_size=... or load bars with symbol metadata."
        )
    levels, reason = value_area_levels_from_ticks(
        df,
        ticks,
        ticks.symbol,
        timeframe=timeframe,
        mode=mode,  # type: ignore[arg-type]
        tick_size=float(resolved_tick),
        row_ticks=row_ticks,
        value_area=value_area,
    )
    levels.attrs["unavailable_reason"] = reason
    return levels


def top_bottom(frame: pd.DataFrame, *, atr_period: int = 14, reversal_atr: float = 2.0) -> pd.DataFrame:
    """Detect confirmed tops and bottoms with a causal ATR-threshold zigzag.

    A pivot is confirmed once price retreats from the running extreme of the current leg
    by at least ``reversal_atr`` times the Wilder ATR. Pivots alternate between tops and
    bottoms and are never revised.

    Parameters
    ----------
    frame : pandas.DataFrame
        Numeric high, low, and close on a chronological index of completed bars.
    atr_period : int, default 14
        Wilder ATR period, an integer >= 1.
    reversal_atr : float, default 2.0
        Reversal size in ATR multiples, a finite number > 0.

    Returns
    -------
    pandas.DataFrame
        Frame on the exact index of ``frame`` with columns:

        - ``top`` / ``bottom``: price of the pivot confirmed on that bar, NaN otherwise.
        - ``top_index`` / ``bottom_index``: index label of the bar holding that pivot
          (object dtype, None otherwise). It is always at or before the confirming bar.
        - ``leg``: detector direction after the bar (1 rising, -1 falling, 0 undefined).

    Raises
    ------
    TypeError
        If ``frame`` is not a DataFrame, ``atr_period`` is not an integer, or
        ``reversal_atr`` is not a number.
    ValueError
        If required columns are missing, values are non-finite, or parameters are out of range.

    Notes
    -----
    Row ``i`` depends only on bars ``0..i``, so the output for a prefix of ``frame`` equals
    the prefix of the output. Pivots are known only on their confirming bar; the lag from
    ``top_index`` / ``bottom_index`` to the row label is the detection latency. Warm-up
    lasts ``atr_period`` bars, during which no pivots are emitted.
    """
    df = _validate_frame(frame, "frame", ("high", "low", "close"))
    period = _validate_window(atr_period, "atr_period", min_val=1)
    reversal = _validate_positive_float(reversal_atr, "reversal_atr")

    detector = LiveSwingDetector(atr_period=period, reversal_atr=reversal)
    n = len(df)
    top = np.full(n, np.nan)
    bottom = np.full(n, np.nan)
    top_index = np.full(n, None, dtype=object)
    bottom_index = np.full(n, None, dtype=object)
    leg = np.zeros(n, dtype=np.int8)

    for pos, (index, high, low, close) in enumerate(
        zip(df.index, df["high"].to_numpy(), df["low"].to_numpy(), df["close"].to_numpy())
    ):
        pivot = detector.update(index, high, low, close)
        if pivot is not None and pivot.kind == "top":
            top[pos] = pivot.price
            top_index[pos] = pivot.pivot_index
        elif pivot is not None:
            bottom[pos] = pivot.price
            bottom_index[pos] = pivot.pivot_index
        leg[pos] = detector.direction

    # Index columns stay object dtype even when they hold timestamps, so prefixes with and
    # without pivots share a dtype.
    return pd.DataFrame(
        {
            "top": top,
            "bottom": bottom,
            "top_index": pd.Series(top_index, index=frame.index, dtype=object),
            "bottom_index": pd.Series(bottom_index, index=frame.index, dtype=object),
            "leg": leg,
        },
        index=frame.index,
    )
