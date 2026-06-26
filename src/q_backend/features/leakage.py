"""Leakage guard helpers for feature computation (WO128)."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

import pandas as pd

if TYPE_CHECKING:
    from q_backend.features.compute import FeatureSeries

# v1: empty — documents the hook for forward-looking primitives.
FORWARD_LOOKING_KINDS: frozenset[str] = frozenset()

_DEFAULT_RTOL = 1e-9
_DEFAULT_ATOL = 1e-9


class LeakageError(AssertionError):
    """Raised when a feature value at bar t changes after future bars are removed."""


def assert_causal(
    compute_fn: Callable[[pd.DataFrame], FeatureSeries],
    bars: pd.DataFrame,
    *,
    sample_indices: list[int],
    rtol: float = _DEFAULT_RTOL,
    atol: float = _DEFAULT_ATOL,
) -> None:
    """Assert feature values at sampled indices depend only on bars ≤ t."""
    full = compute_fn(bars)
    n = len(bars)
    for t in sample_indices:
        if t < 0 or t >= n:
            raise ValueError(f"sample index {t} out of range for {n} bars")
        prefix = bars.iloc[: t + 1].copy()
        partial = compute_fn(prefix)
        full_val = full.series.iloc[t]
        partial_val = partial.series.iloc[t]
        if pd.isna(full_val) and pd.isna(partial_val):
            continue
        if pd.isna(full_val) != pd.isna(partial_val):
            raise LeakageError(
                f"Leakage at index {t}: full={full_val!r}, prefix={partial_val!r}"
            )
        if abs(float(full_val) - float(partial_val)) > atol + rtol * abs(float(full_val)):
            raise LeakageError(
                f"Leakage at index {t}: full={full_val!r}, prefix={partial_val!r}"
            )


def leaky_close_shift_feature(bars: pd.DataFrame) -> FeatureSeries:
    """Deliberately forward-looking helper for leakage tests."""
    from q_backend.features.compute import FeatureSeries

    df = bars.sort_values("time").reset_index(drop=True)
    series = pd.Series(
        df["close"].shift(-1).to_numpy(),
        index=df["time"],
        name="leaky_close_shift",
    )
    return FeatureSeries(
        feature_id="leaky_close_shift.v1.test",
        series=series,
        warmup_bars=0,
        leakage_status="suspect",
    )
