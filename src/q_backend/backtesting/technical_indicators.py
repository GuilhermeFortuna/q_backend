from __future__ import annotations

import numpy as np
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


def _require_positive_float(param: str, value: float) -> float:
    if value <= 0.0 or not float(value) == value:
        raise ValueError(f"{param} must be a finite number > 0 (got {value}).")
    return float(value)


PerBarVolEstimator = str


def compute_log_return(close: pd.Series, horizon: int) -> pd.Series:
    require_window("horizon", horizon, 1)
    res = kernels.log_return(as_float64(close), horizon)
    return to_series(res, close.index, close.name)


def compute_realized_vol_per_bar(close: pd.Series, window: int) -> pd.Series:
    require_window("window", window, 1)
    res = kernels.realized_vol_per_bar(as_float64(close), window)
    return to_series(res, close.index, close.name)


def compute_ewma_vol_per_bar(close: pd.Series, half_life: float) -> pd.Series:
    hl = _require_positive_float("half_life", half_life)
    res = kernels.ewma_vol_per_bar(as_float64(close), hl)
    return to_series(res, close.index, close.name)


def compute_rogers_satchell(
    open_: pd.Series,
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    window: int,
) -> pd.Series:
    require_window("window", window, 1)
    idx = shared_index(close, open_, high, low)
    res = kernels.rogers_satchell(
        as_float64(open_),
        as_float64(high),
        as_float64(low),
        as_float64(close),
        window,
    )
    return to_series(res, idx, None)


def compute_true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    idx = shared_index(close, high, low)
    res = kernels.true_range(as_float64(high), as_float64(low), as_float64(close))
    return to_series(res, idx, None)


def compute_atr_fraction(high: pd.Series, low: pd.Series, close: pd.Series, period: int) -> pd.Series:
    require_window("period", period, 1)
    idx = shared_index(close, high, low)
    res = kernels.atr_fraction(as_float64(high), as_float64(low), as_float64(close), period)
    return to_series(res, idx, None)


def compute_price_zscore_log(close: pd.Series, window: int) -> pd.Series:
    require_window("window", window, 2)
    res = kernels.price_zscore_log(as_float64(close), window)
    return to_series(res, close.index, close.name)


def compute_ewma_trend_spread(close: pd.Series, fast_half_life: float, slow_half_life: float) -> pd.Series:
    fast = _require_positive_float("fast_half_life", fast_half_life)
    slow = _require_positive_float("slow_half_life", slow_half_life)
    if fast >= slow:
        raise ValueError(f"fast_half_life must be < slow_half_life (got {fast} >= {slow}).")
    res = kernels.ewma_trend_spread(as_float64(close), fast, slow)
    return to_series(res, close.index, close.name)


def compute_vol_normalize(
    values: pd.Series,
    sigma: pd.Series,
    horizon: int,
    vol_lag: int,
) -> pd.Series:
    require_window("horizon", horizon, 1)
    if vol_lag not in (0, 1):
        raise ValueError(f"vol_lag must be 0 or 1 (got {vol_lag}).")
    idx = shared_index(values, sigma)
    res = kernels.vol_normalize(as_float64(values), as_float64(sigma), horizon, vol_lag)
    return to_series(res, idx, values.name)


def compute_volatility_log_ratio(sigma_short: pd.Series, sigma_long: pd.Series) -> pd.Series:
    idx = shared_index(sigma_short, sigma_long)
    res = kernels.volatility_log_ratio(as_float64(sigma_short), as_float64(sigma_long))
    return to_series(res, idx, None)


def compute_per_bar_volatility(
    open_: pd.Series,
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    *,
    estimator: PerBarVolEstimator,
    window: int | None = None,
    half_life: float | None = None,
) -> pd.Series:
    normalized = estimator.lower().strip()
    if normalized == "realized":
        if window is None:
            raise ValueError("window is required for realized per-bar volatility.")
        return compute_realized_vol_per_bar(close, window)
    if normalized == "ewma":
        if half_life is None:
            raise ValueError("half_life is required for ewma per-bar volatility.")
        return compute_ewma_vol_per_bar(close, half_life)
    if normalized == "rogers_satchell":
        if window is None:
            raise ValueError("window is required for rogers_satchell per-bar volatility.")
        return compute_rogers_satchell(open_, high, low, close, window)
    if normalized == "yang_zhang":
        if window is None:
            raise ValueError("window is required for yang_zhang per-bar volatility.")
        return compute_yang_zhang(open_, high, low, close, window, periods_per_year=1)
    allowed = "realized, ewma, rogers_satchell, yang_zhang"
    raise ValueError(f"Invalid vol_estimator '{estimator}'. Must be one of: {allowed}")


def compute_normalized_momentum(
    close: pd.Series,
    open_: pd.Series,
    high: pd.Series,
    low: pd.Series,
    *,
    horizon: int,
    vol_estimator: PerBarVolEstimator,
    vol_window: int | None,
    vol_half_life: float | None,
    vol_lag: int,
) -> pd.Series:
    momentum = compute_log_return(close, horizon)
    sigma = compute_per_bar_volatility(
        open_,
        high,
        low,
        close,
        estimator=vol_estimator,
        window=vol_window,
        half_life=vol_half_life,
    )
    return compute_vol_normalize(momentum, sigma, horizon, vol_lag)


def compute_normalized_ewma_trend_spread(
    close: pd.Series,
    open_: pd.Series,
    high: pd.Series,
    low: pd.Series,
    *,
    fast_half_life: float,
    slow_half_life: float,
    vol_estimator: PerBarVolEstimator,
    vol_window: int | None,
    vol_half_life: float | None,
    vol_lag: int,
) -> pd.Series:
    spread = compute_ewma_trend_spread(close, fast_half_life, slow_half_life)
    sigma = compute_per_bar_volatility(
        open_,
        high,
        low,
        close,
        estimator=vol_estimator,
        window=vol_window,
        half_life=vol_half_life,
    )
    return compute_vol_normalize(spread, sigma, 1, vol_lag)


def compute_quote_spread_bars(
    time_msc: np.ndarray,
    bid: np.ndarray,
    ask: np.ndarray,
    flags: np.ndarray,
    session: np.ndarray,
    bar_open_msc: np.ndarray,
    bar_close_msc: np.ndarray,
    max_quote_age_ms: int,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Return ``(last, time_weighted_mean, max, coverage)`` arrays from q_core."""
    if max_quote_age_ms < 0:
        raise ValueError(f"max_quote_age_ms must be >= 0 (got {max_quote_age_ms}).")
    last, twm, mx, coverage = kernels.quote_spread_bars(
        np.ascontiguousarray(time_msc, dtype=np.int64),
        np.ascontiguousarray(bid, dtype=np.float64),
        np.ascontiguousarray(ask, dtype=np.float64),
        np.ascontiguousarray(flags, dtype=np.int64),
        np.ascontiguousarray(session, dtype=np.int64),
        np.ascontiguousarray(bar_open_msc, dtype=np.int64),
        np.ascontiguousarray(bar_close_msc, dtype=np.int64),
        int(max_quote_age_ms),
    )
    return last, twm, mx, coverage
