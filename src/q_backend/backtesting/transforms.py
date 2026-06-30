"""Causal series transforms shared by genome evaluation and the Feature Store (WO158)."""

from __future__ import annotations

import numpy as np
import pandas as pd


def compute_rolling_zscore(series: pd.Series, window: int) -> pd.Series:
    """Trailing z-score using population std (matches ``compute_bollinger_bands``)."""
    roll = series.rolling(window=window, min_periods=window)
    mean = roll.mean()
    std = roll.std()
    return (series - mean) / std


def compute_rolling_rank(series: pd.Series, window: int) -> pd.Series:
    """Percentile rank of the current value within its trailing window (0–1)."""
    values = series.to_numpy(dtype=float)
    out = np.full(len(values), np.nan)
    for idx in range(len(values)):
        start = idx - window + 1
        if start < 0:
            continue
        window_vals = values[start : idx + 1]
        if np.isnan(window_vals).any():
            continue
        current = window_vals[-1]
        out[idx] = float(np.sum(window_vals <= current) / window)
    return pd.Series(out, index=series.index)


def compute_pct_change(series: pd.Series, change_bars: int) -> pd.Series:
    """Causal percent change over a positive lag."""
    lag = int(change_bars)
    if lag < 1:
        raise ValueError(f"change_bars must be >= 1 (got {lag}).")
    return series / series.shift(lag) - 1.0


def compute_clip(series: pd.Series, clip_low: float, clip_high: float) -> pd.Series:
    """Clamp values to ``[clip_low, clip_high]``."""
    low = float(clip_low)
    high = float(clip_high)
    if low > high:
        raise ValueError(f"clip_low must be <= clip_high (got {low} > {high}).")
    return series.clip(lower=low, upper=high)
