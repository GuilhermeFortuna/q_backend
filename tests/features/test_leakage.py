"""Tests for feature leakage guards (WO128)."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from datetime import datetime, timezone

from q_backend.features.compute import compute_feature
from q_backend.features.leakage import (
    LeakageError,
    assert_causal,
    assert_neural_oos_only,
    leaky_close_shift_feature,
    neural_leakage_status,
)
from q_backend.features.registry import get_feature_spec, list_feature_specs, resolve_params


def _synthetic_bars(n: int = 260) -> pd.DataFrame:
    rng = np.random.default_rng(20240609)
    steps = rng.normal(0.0, 1.0, size=n)
    close = 100.0 + np.cumsum(steps)
    open_ = close - rng.normal(0.0, 0.5, size=n)
    high = np.maximum(open_, close) + np.abs(rng.normal(0.0, 0.5, size=n))
    low = np.minimum(open_, close) - np.abs(rng.normal(0.0, 0.5, size=n))
    volume = rng.integers(1_000, 5_000, size=n).astype(float)
    times = pd.date_range("2023-01-01", periods=n, freq="h", tz="UTC")
    return pd.DataFrame(
        {
            "time": times,
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
        }
    )


@pytest.mark.parametrize("spec_name", [spec.name for spec in list_feature_specs()])
def test_assert_causal_passes_for_v1_specs(spec_name: str) -> None:
    bars = _synthetic_bars()
    spec = get_feature_spec(spec_name)
    params = resolve_params(spec, {})
    sample = [50, 120, 200, len(bars) - 1]

    def _compute(frame: pd.DataFrame):
        return compute_feature(frame, spec, params)

    assert_causal(_compute, bars, sample_indices=sample)


def test_assert_causal_rejects_leaky_shift() -> None:
    bars = _synthetic_bars()
    with pytest.raises(LeakageError, match="Leakage at index 100"):
        assert_causal(leaky_close_shift_feature, bars, sample_indices=[100])


def test_leaky_shift_only_affects_interior_bars() -> None:
    bars = _synthetic_bars(120)
    full = leaky_close_shift_feature(bars)
    assert pd.isna(full.series.iloc[-1])
    assert full.series.iloc[0] == pytest.approx(bars["close"].iloc[1])


def _naive_times(n: int = 10) -> pd.Series:
    """Bar times as the data lake delivers them: tz-naive ``datetime64``."""
    return pd.Series(pd.date_range("2026-01-01", periods=n, freq="h"))


def test_neural_leakage_status_handles_tz_naive_bar_times() -> None:
    # The data lake yields tz-naive datetime64 while train_end is tz-aware UTC.
    # Comparing the two directly raises "Invalid comparison" — the OOS gate path.
    times = _naive_times()  # 2026-01-01 00:00..09:00, all strictly after train_end
    series = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0])
    train_end = datetime(2025, 12, 31, 23, 0, tzinfo=timezone.utc)
    assert neural_leakage_status(times, series, train_end) == "clean"


def test_assert_neural_oos_only_handles_tz_naive_bar_times() -> None:
    times = _naive_times()
    # All latents land strictly after train_end -> no leak, must not raise on dtype.
    series = pd.Series([float("nan")] * 5 + [1.0, 2.0, 3.0, 4.0, 5.0])
    train_end = datetime(2026, 1, 1, 4, 0, tzinfo=timezone.utc)
    assert_neural_oos_only(series, times, train_end)
