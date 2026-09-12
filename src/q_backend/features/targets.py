"""Target / label definitions for feature evaluation (WO132).

Targets are the **only** place forward-looking ``.shift(-horizon)`` is permitted.
Features must remain causal (``forward_window == 0``); see ``compute.py``.

Embargo: ``embargo_bars(spec) == spec.horizon``. When splitting train/test at
``split_point``, call ``purge_embargo`` to drop the last ``embargo`` train rows and
the first ``embargo`` test rows so labels built from future bars cannot straddle
the boundary (WO133 uses this for leakage-safe evaluation).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import numpy as np
import pandas as pd

from q_backend.backtesting.technical_indicators import compute_realized_vol

TargetKind = Literal["regression", "classification"]
TargetName = Literal[
    "fwd_return",
    "fwd_log_return",
    "fwd_vol_adj_return",
    "fwd_direction",
]

_TARGET_KINDS: dict[str, TargetKind] = {
    "fwd_return": "regression",
    "fwd_log_return": "regression",
    "fwd_vol_adj_return": "regression",
    "fwd_direction": "classification",
}

# Denominator window for vol-adjusted returns (causal realized vol at t).
DEFAULT_VOL_WINDOW = 63

_REQUIRED_BAR_COLUMNS = frozenset({"time", "close"})


@dataclass(frozen=True)
class TargetSpec:
    name: str
    horizon: int
    kind: str


def _validate_bars(bars: pd.DataFrame) -> None:
    missing = _REQUIRED_BAR_COLUMNS - set(bars.columns)
    if missing:
        raise ValueError(f"bars missing required columns: {sorted(missing)}")
    if not bars["time"].is_monotonic_increasing:
        raise ValueError("bars must be time-sorted ascending")


def list_target_specs(horizons: list[int]) -> list[TargetSpec]:
    specs: list[TargetSpec] = []
    for horizon in sorted(horizons):
        if horizon <= 0:
            raise ValueError(f"horizon must be positive, got {horizon}")
        for name in _TARGET_KINDS:
            specs.append(TargetSpec(name=name, horizon=horizon, kind=_TARGET_KINDS[name]))
    return specs


def embargo_bars(spec: TargetSpec) -> int:
    return spec.horizon


def _fwd_return(close: pd.Series, horizon: int) -> pd.Series:
    return close.shift(-horizon) / close - 1.0


def compute_target(bars: pd.DataFrame, spec: TargetSpec) -> pd.Series:
    """Compute a forward-looking label series indexed by bar time."""
    _validate_bars(bars)
    if spec.horizon <= 0:
        raise ValueError(f"horizon must be positive, got {spec.horizon}")
    if spec.name not in _TARGET_KINDS:
        raise ValueError(f"Unknown target name '{spec.name}'.")

    df = bars.sort_values("time").reset_index(drop=True)
    close = df["close"]
    horizon = spec.horizon

    if spec.name == "fwd_return":
        values = _fwd_return(close, horizon)
    elif spec.name == "fwd_log_return":
        values = np.log(close.shift(-horizon) / close)
    elif spec.name == "fwd_vol_adj_return":
        ret = _fwd_return(close, horizon)
        vol = compute_realized_vol(close, DEFAULT_VOL_WINDOW)
        values = ret / vol
    elif spec.name == "fwd_direction":
        ret = _fwd_return(close, horizon)
        values = pd.Series(np.sign(ret.to_numpy()), index=ret.index, dtype=float)
        values = values.where(ret.notna(), np.nan)
    else:
        raise ValueError(f"Unknown target name '{spec.name}'.")

    return pd.Series(values.to_numpy(), index=df["time"], name=spec.name)


def purge_embargo(
    index: pd.Index,
    split_point: int,
    embargo: int,
) -> tuple[pd.Index, pd.Index]:
    """Return train/test index slices with ``embargo`` bars purged at the split."""
    if embargo < 0:
        raise ValueError(f"embargo must be non-negative, got {embargo}")
    if split_point < 0 or split_point > len(index):
        raise ValueError(f"split_point {split_point} out of range for index length {len(index)}")

    train_end = max(0, split_point - embargo)
    test_start = min(len(index), split_point + embargo)
    train_idx = index[:train_end]
    test_idx = index[test_start:]
    return train_idx, test_idx


def align_feature_target(
    feature: pd.Series,
    target: pd.Series,
) -> tuple[pd.Series, pd.Series]:
    """Inner-join on time and drop rows where either series is NaN."""
    frame = pd.concat(
        [feature.rename("feature"), target.rename("target")],
        axis=1,
        join="inner",
    )
    frame = frame.dropna()
    return frame["feature"], frame["target"]
