"""Tests for feature leakage guards (WO128)."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from q_backend.features.compute import compute_feature
from q_backend.features.leakage import LeakageError, assert_causal, leaky_close_shift_feature
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
