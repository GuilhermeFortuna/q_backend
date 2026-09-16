"""Bridge module connecting q_backend to q_core indicator kernels.

q_core is imported only by bridge modules (this file and
``backtesting/tick/kernel.py``).
"""

from __future__ import annotations

from typing import Any, Hashable
import numpy as np
import pandas as pd
import q_core

REQUIRED_KERNELS: tuple[str, ...] = (
    "realized_vol",
    "yang_zhang",
    "rsi",
    "bollinger_bands",
    "macd",
    "donchian_channels",
    "atr",
    "sma",
    "ema",
    "smma",
    "wma",
    "hma",
    "rolling_zscore",
    "rolling_rank",
    "pct_change",
    "clip",
)


def check_kernels(module: Any) -> None:
    """Raise ImportError naming every missing REQUIRED_KERNELS entry and module.version()."""
    target = getattr(module, "indicators", module)
    ver_fn = getattr(module, "version", None)
    if callable(ver_fn):
        version = ver_fn()
    else:
        version = getattr(module, "__version__", "unknown")

    missing = [name for name in REQUIRED_KERNELS if not hasattr(target, name)]
    if missing:
        raise ImportError(f"q_core {version} is missing required kernels: {', '.join(missing)}")


# Validate installed q_core at import time
check_kernels(q_core)

# Expose kernel functions through this bridge
kernels: Any = getattr(q_core, "indicators", q_core)


def as_float64(series: pd.Series) -> np.ndarray:
    """Contiguous float64 values. No copy when already contiguous float64; pd.NA -> NaN."""
    arr = series.to_numpy(dtype=np.float64, na_value=np.nan)
    if not arr.flags["C_CONTIGUOUS"]:
        arr = np.ascontiguousarray(arr)
    return arr


def shared_index(*series: pd.Series) -> pd.Index:
    """The common index object; ValueError if lengths or labels differ (identity short-circuits)."""
    if not series:
        raise ValueError("At least one series required.")
    first_idx = series[0].index
    for s in series[1:]:
        if s.index is first_idx:
            continue
        if len(s.index) != len(first_idx) or not s.index.equals(first_idx):
            raise ValueError("Input series must share an identical index.")
    return first_idx


def to_series(values: np.ndarray, index: pd.Index, name: Hashable | None) -> pd.Series:
    """Wrap a q_core result without copying."""
    return pd.Series(values, index=index, name=name, copy=False)


def require_window(param: str, value: int, minimum: int) -> int:
    """ValueError f"{param} must be >= {minimum} (got {value})." for out-of-domain windows."""
    if value < minimum:
        raise ValueError(f"{param} must be >= {minimum} (got {value}).")
    return value
