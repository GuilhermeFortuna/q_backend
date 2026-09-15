"""Causal series transforms shared by genome evaluation and the Feature Store (WO158)."""

from __future__ import annotations

import pandas as pd

from q_backend.backtesting.indicator_kernels import (
    as_float64,
    kernels,
    require_window,
    to_series,
)


def compute_rolling_zscore(series: pd.Series, window: int) -> pd.Series:
    """Trailing z-score using sample std (ddof=1) (matches ``compute_bollinger_bands``)."""
    require_window("window", window, 1)
    res = kernels.rolling_zscore(as_float64(series), window)
    if str(series.dtype) == "Float64":
        return pd.Series(res, index=series.index, dtype=series.dtype, name=series.name)
    return to_series(res, series.index, series.name)


def compute_rolling_rank(series: pd.Series, window: int) -> pd.Series:
    """Percentile rank of the current value within its trailing window (0–1)."""
    require_window("window", window, 1)
    res = kernels.rolling_rank(as_float64(series), window)
    return to_series(res, series.index, None)


def compute_pct_change(series: pd.Series, change_bars: int) -> pd.Series:
    """Causal percent change over a positive lag."""
    lag = int(change_bars)
    if lag < 1:
        raise ValueError(f"change_bars must be >= 1 (got {lag}).")
    res = kernels.pct_change(as_float64(series), lag)
    if str(series.dtype) == "Float64":
        return pd.Series(res, index=series.index, dtype=series.dtype, name=series.name)
    return to_series(res, series.index, series.name)


def compute_clip(series: pd.Series, clip_low: float, clip_high: float) -> pd.Series:
    """Clamp values to ``[clip_low, clip_high]``."""
    low = float(clip_low)
    high = float(clip_high)
    if low > high:
        raise ValueError(f"clip_low must be <= clip_high (got {low} > {high}).")
    res = getattr(kernels, "clip")(as_float64(series), low, high)
    if str(series.dtype) == "Float64":
        return pd.Series(res, index=series.index, dtype=series.dtype, name=series.name)
    if pd.api.types.is_integer_dtype(series.dtype) and low.is_integer() and high.is_integer():
        res = res.astype(series.dtype)
    return to_series(res, series.index, series.name)
