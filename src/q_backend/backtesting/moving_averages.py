from __future__ import annotations

from typing import Literal

import pandas as pd

from q_backend.backtesting.indicator_kernels import as_float64, kernels, to_series

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
    arr = as_float64(series)

    if normalized == "sma":
        res = kernels.sma(arr, period)
    elif normalized == "ema":
        res = kernels.ema(arr, period)
    elif normalized == "wma":
        res = kernels.wma(arr, period)
    elif normalized == "smma":
        res = kernels.smma(arr, period)
    elif normalized == "hma":
        res = kernels.hma(arr, period)
    else:
        raise ValueError(f"Unsupported MA type: {ma_type}")

    return to_series(res, series.index, series.name)
