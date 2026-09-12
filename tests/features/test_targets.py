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


def _varied_bars(n: int = 120) -> pd.DataFrame:
    """Non-monotonic close so forward perturbations reliably move the target."""
    rng = np.random.default_rng(1181)
    close = 100.0 + np.cumsum(rng.normal(0.0, 1.0, size=n))
    open_ = close - 0.1
    high = np.maximum(open_, close) + 0.5
    low = np.minimum(open_, close) - 0.5
    volume = np.full(n, 1000.0)
    times = pd.date_range("2023-01-01", periods=n, freq="h", tz="UTC")
    return pd.DataFrame({"time": times, "open": open_, "high": high, "low": low, "close": close, "volume": volume})


def _perturb_close(bars: pd.DataFrame, index: int, delta: float) -> pd.DataFrame:
    perturbed = bars.copy()
    perturbed.loc[perturbed.index[index], "close"] = bars["close"].iloc[index] + delta
    return perturbed


@pytest.mark.parametrize("name", ["fwd_return", "fwd_log_return", "fwd_vol_adj_return", "fwd_direction"])
def test_target_does_not_depend_on_bars_beyond_horizon(name: str) -> None:
    """A target at bar t must be forward-looking only *up to* its horizon (no bleed).

    Perturbing any bar strictly beyond ``t + horizon`` must leave target[t] unchanged.
    """
    bars = _varied_bars()
    horizon = 5
    t = 60
    spec = TargetSpec(name, horizon, _TARGET_KIND(name))
    base = compute_target(bars, spec).iloc[t]

    for beyond in (t + horizon + 1, t + horizon + 10, len(bars) - 1):
        perturbed = _perturb_close(bars, beyond, delta=25.0)
        after = compute_target(perturbed, spec).iloc[t]
        if pd.isna(base):
            assert pd.isna(after)
        else:
            assert after == pytest.approx(base), f"{name}[{t}] changed after perturbing bar {beyond} > t+horizon"


@pytest.mark.parametrize("name", ["fwd_return", "fwd_log_return"])
def test_target_depends_on_the_horizon_bar(name: str) -> None:
    """The forward window genuinely reaches the horizon bar (t + horizon)."""
    bars = _varied_bars()
    horizon = 5
    t = 60
    spec = TargetSpec(name, horizon, _TARGET_KIND(name))
    base = compute_target(bars, spec).iloc[t]
    moved = compute_target(_perturb_close(bars, t + horizon, delta=25.0), spec).iloc[t]
    assert moved != pytest.approx(base)


def _TARGET_KIND(name: str) -> str:
    return "classification" if name == "fwd_direction" else "regression"


def test_training_eligible_labels_do_not_bleed_past_train_end() -> None:
    """Rows within ``horizon`` of the split are purged, so no train label reaches test.

    With ``embargo == horizon``, every training-eligible bar t satisfies
    ``t + horizon < split_point`` — the label window never crosses the train/test boundary.
    """
    bars = _varied_bars(160)
    horizon = 6
    split_point = 100
    embargo = horizon
    times = pd.Index(bars["time"])
    train_idx, test_idx = purge_embargo(times, split_point, embargo)

    position = {ts: pos for pos, ts in enumerate(times)}
    train_positions = [position[ts] for ts in train_idx]
    assert train_positions, "expected a non-empty training-eligible set"
    for t in train_positions:
        assert t + horizon < split_point, f"train label at bar {t} reaches {t + horizon} >= split_point {split_point}"

    first_test_pos = position[test_idx[0]]
    assert first_test_pos >= split_point + embargo


def test_forward_window_checker_catches_a_beyond_horizon_leak() -> None:
    """A deliberately leaky target (peeks past its horizon) fails the forward-window rule."""
    bars = _varied_bars()
    horizon = 5
    t = 60

    def _leaky_target(frame: pd.DataFrame) -> pd.Series:
        # Peeks one bar past the stated horizon — the target equivalent of shift(-1) leak.
        close = frame.sort_values("time")["close"].reset_index(drop=True)
        values = close.shift(-(horizon + 1)) / close - 1.0
        return pd.Series(values.to_numpy(), index=frame["time"])

    base = _leaky_target(bars).iloc[t]
    beyond = t + horizon + 1  # inside the leaky window, beyond the declared horizon
    after = _leaky_target(_perturb_close(bars, beyond, delta=25.0)).iloc[t]
    assert after != pytest.approx(base), "leaky target should react to a beyond-horizon bar"


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
