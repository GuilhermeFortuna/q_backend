"""Tests for target / label definitions (WO132)."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from q_backend.features.compute import compute_feature
from q_backend.features.registry import get_feature_spec
from q_backend.features.targets import (
    TargetSpec,
    align_feature_target,
    compute_target,
    embargo_bars,
    list_target_specs,
    purge_embargo,
)


def _synthetic_bars(n: int = 50) -> pd.DataFrame:
    close = np.linspace(100.0, 100.0 + n - 1, n)
    open_ = close - 0.1
    high = close + 0.5
    low = close - 0.5
    volume = np.full(n, 1000.0)
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


def test_fwd_return_matches_manual_formula_and_trailing_nan():
    bars = _synthetic_bars(20)
    horizon = 3
    spec = TargetSpec("fwd_return", horizon, "regression")
    target = compute_target(bars, spec)

    close = bars["close"]
    for t in range(len(bars) - horizon):
        expected = close.iloc[t + horizon] / close.iloc[t] - 1.0
        assert target.iloc[t] == pytest.approx(expected)

    assert target.iloc[-horizon:].isna().all()
    assert target.iloc[: len(bars) - horizon].notna().all()


def test_fwd_direction_values_in_minus_one_zero_one():
    bars = _synthetic_bars(30)
    spec = TargetSpec("fwd_direction", 5, "classification")
    target = compute_target(bars, spec)

    valid = target.dropna()
    assert set(valid.unique()).issubset({-1.0, 0.0, 1.0})


def test_embargo_bars_equals_horizon():
    spec = TargetSpec("fwd_return", 12, "regression")
    assert embargo_bars(spec) == 12


def test_purge_embargo_removes_overlap():
    index = pd.RangeIndex(0, 100)
    split_point = 60
    embargo = 5
    train_idx, test_idx = purge_embargo(index, split_point, embargo)

    assert len(train_idx) == split_point - embargo
    assert len(test_idx) == len(index) - split_point - embargo
    assert train_idx[-1] == split_point - embargo - 1
    assert test_idx[0] == split_point + embargo
    assert set(train_idx).isdisjoint(set(test_idx))


def test_purge_embargo_prevents_label_overlap_for_horizon():
    """A label at the last purged train bar must not use bars inside the test window."""
    bars = _synthetic_bars(40)
    horizon = 4
    split_point = 25
    embargo = horizon
    train_idx, test_idx = purge_embargo(bars["time"], split_point, embargo)

    last_train_pos = split_point - embargo - 1
    first_test_pos = split_point + embargo
    # fwd_return at t uses close[t + horizon]; must not reach into test segment.
    assert last_train_pos + horizon < first_test_pos
    assert len(train_idx) == split_point - embargo
    assert len(test_idx) == len(bars) - split_point - embargo


def test_align_feature_target_drops_warmup_and_horizon_tail():
    bars = _synthetic_bars(60)
    horizon = 5
    feature_spec = get_feature_spec("rsi")
    feature = compute_feature(bars, feature_spec, {"period": 14}).series
    target = compute_target(bars, TargetSpec("fwd_return", horizon, "regression"))

    aligned_feature, aligned_target = align_feature_target(feature, target)

    assert len(aligned_feature) == len(aligned_target)
    assert aligned_feature.index.equals(aligned_target.index)
    assert aligned_feature.notna().all()
    assert aligned_target.notna().all()
    assert len(aligned_feature) < len(bars)


def test_list_target_specs_expands_horizons():
    specs = list_target_specs([1, 5])
    names = {spec.name for spec in specs}
    horizons = {spec.horizon for spec in specs}
    assert names == {
        "fwd_return",
        "fwd_log_return",
        "fwd_vol_adj_return",
        "fwd_direction",
    }
    assert horizons == {1, 5}
    assert len(specs) == 8
