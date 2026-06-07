from typing import Literal

import numpy as np
import pandas as pd

MaType = Literal["sma", "ema", "wma", "smma", "hma"]

VALID_MA_TYPES: frozenset[str] = frozenset({"sma", "ema", "wma", "smma", "hma"})

MA_TYPE_LABELS: dict[str, str] = {
    "sma": "SMA",
    "ema": "EMA",
    "wma": "WMA",
    "smma": "SMMA",
    "hma": "HMA",
}


def normalize_ma_type(value: object, default: str = "sma") -> str:
    normalized = str(value).lower().strip()
    if normalized not in VALID_MA_TYPES:
        allowed = ", ".join(sorted(VALID_MA_TYPES))
        raise ValueError(f"Invalid MA type '{value}'. Must be one of: {allowed}")
    return normalized


def compute_ma(series: pd.Series, period: int, ma_type: str) -> pd.Series:
    if period < 1:
        raise ValueError("MA period must be at least 1.")

    normalized = normalize_ma_type(ma_type)

    if normalized == "sma":
        return series.rolling(window=period).mean()
    if normalized == "ema":
        return series.ewm(span=period, adjust=False).mean()
    if normalized == "wma":
        return _wma(series, period)
    if normalized == "smma":
        return series.ewm(alpha=1 / period, adjust=False).mean()
    if normalized == "hma":
        return _hma(series, period)

    raise ValueError(f"Unsupported MA type: {ma_type}")


def _wma(series: pd.Series, period: int) -> pd.Series:
    weights = np.arange(1, period + 1, dtype=float)
    weight_sum = weights.sum()

    def weighted_mean(window: np.ndarray) -> float:
        if len(window) < period:
            return np.nan
        return float(np.dot(window, weights) / weight_sum)

    return series.rolling(window=period).apply(weighted_mean, raw=True)


def _hma(series: pd.Series, period: int) -> pd.Series:
    half_period = max(period // 2, 1)
    sqrt_period = max(int(np.sqrt(period)), 1)
    wma_half = _wma(series, half_period)
    wma_full = _wma(series, period)
    raw = 2 * wma_half - wma_full
    return _wma(raw, sqrt_period)
