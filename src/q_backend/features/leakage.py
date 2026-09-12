"""Leakage guard helpers for feature computation (WO128, WO143)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING

import pandas as pd

if TYPE_CHECKING:
    from q_backend.features.compute import FeatureSeries
    from q_backend.features.registry import FeatureSpec

# v1: empty — documents the hook for forward-looking primitives.
FORWARD_LOOKING_KINDS: frozenset[str] = frozenset()

_DEFAULT_RTOL = 1e-9
_DEFAULT_ATOL = 1e-9


class LeakageError(AssertionError):
    """Raised when a feature value at bar t changes after future bars are removed."""


def _to_utc_timestamp(value: datetime | pd.Timestamp) -> pd.Timestamp:
    ts = pd.Timestamp(value)
    if ts.tzinfo is None:
        return ts.tz_localize("UTC")
    return ts.tz_convert("UTC")


def to_utc_series(times: pd.Series) -> pd.Series:
    """Normalize a datetime Series to tz-aware UTC for comparison with ``train_end``.

    The data lake delivers tz-naive ``datetime64`` bar times, but ``train_end`` is
    tz-aware UTC. Comparing the two directly raises ``Invalid comparison``; localize
    naive series to UTC (and convert aware ones) so the comparison is well-defined.
    """
    converted = pd.to_datetime(times)
    if converted.dt.tz is None:
        return converted.dt.tz_localize("UTC")
    return converted.dt.tz_convert("UTC")


def neural_leakage_status(
    times: pd.Series,
    series: pd.Series,
    train_end: datetime,
) -> str:
    """Neural latents are clean only when the request range is entirely OOS."""
    train_end_ts = _to_utc_timestamp(train_end)
    aligned_times = to_utc_series(times.reset_index(drop=True))
    aligned_series = series.reset_index(drop=True)
    if (aligned_times <= train_end_ts).any():
        return "suspect"
    non_nan = aligned_series.notna()
    if not non_nan.any():
        return "suspect"
    if (aligned_times[non_nan.to_numpy()] <= train_end_ts).any():
        return "suspect"
    return "clean"


def assert_neural_oos_only(
    series: pd.Series,
    times: pd.Series,
    train_end: datetime,
) -> None:
    """Fail if any non-NaN latent exists at or before ``train_end``."""
    train_end_ts = _to_utc_timestamp(train_end)
    leaked = series.notna() & (to_utc_series(times) <= train_end_ts)
    if leaked.any():
        first = int(leaked.to_numpy().nonzero()[0][0])
        raise LeakageError(f"Neural latent leak at index {first}: non-NaN value at or before train_end.")


def assert_causal(
    compute_fn: Callable[[pd.DataFrame], FeatureSeries],
    bars: pd.DataFrame,
    *,
    sample_indices: list[int],
    spec: FeatureSpec | None = None,
    train_end: datetime | None = None,
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
            raise LeakageError(f"Leakage at index {t}: full={full_val!r}, prefix={partial_val!r}")
        if abs(float(full_val) - float(partial_val)) > atol + rtol * abs(float(full_val)):
            raise LeakageError(f"Leakage at index {t}: full={full_val!r}, prefix={partial_val!r}")

    if spec is not None and spec.source == "neural":
        if train_end is None:
            raise ValueError("train_end is required for neural assert_causal checks")
        times = bars.sort_values("time")["time"].reset_index(drop=True)
        assert_neural_oos_only(full.series.reset_index(drop=True), times, train_end)


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
