"""Tests for feature evaluation metrics (WO133)."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from q_backend.features.evaluation import (
    MIN_OBS,
    FeatureEvaluation,
    assign_regime_buckets,
    evaluate_feature,
    evaluate_matrix,
)
from q_backend.features.matrix import FeatureMatrix
from q_backend.features.targets import TargetSpec, compute_target
from q_backend.backtesting.technical_indicators import compute_realized_vol


def _synthetic_bars(n: int = 300) -> pd.DataFrame:
    rng = np.random.default_rng(42)
    steps = rng.normal(0.0, 0.5, size=n)
    close = 100.0 + np.cumsum(steps)
    open_ = close - rng.normal(0.0, 0.1, size=n)
    high = np.maximum(open_, close) + np.abs(rng.normal(0.0, 0.2, size=n))
    low = np.minimum(open_, close) - np.abs(rng.normal(0.0, 0.2, size=n))
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


def _aligned_target_and_close(bars: pd.DataFrame, horizon: int = 5):
    target = compute_target(bars, TargetSpec("fwd_return", horizon, "regression"))
    close = pd.Series(bars["close"].to_numpy(), index=bars["time"], name="close")
    return target, close


def test_noisy_target_feature_scores_high_ic():
    bars = _synthetic_bars(300)
    target, close = _aligned_target_and_close(bars)
    rng = np.random.default_rng(0)
    noise = pd.Series(rng.normal(0, 0.001, len(target)), index=target.index)
    feature = (target + noise).rename("signal_feature")

    result = evaluate_feature(feature, target, close=close)
    assert result.n_obs >= MIN_OBS
    assert result.ic > 0.9
    assert result.rank_ic > 0.9


def test_pure_noise_feature_scores_near_zero_ic():
    bars = _synthetic_bars(300)
    target, close = _aligned_target_and_close(bars)
    rng = np.random.default_rng(99)
    noise = pd.Series(rng.normal(0, 1.0, len(target)), index=target.index, name="noise")

    result = evaluate_feature(noise, target, close=close)
    assert result.n_obs >= MIN_OBS
    assert abs(result.ic) < 0.15
    assert abs(result.rank_ic) < 0.15


def test_stability_higher_for_uniform_than_regime_only_feature():
    bars = _synthetic_bars(360)
    target, close = _aligned_target_and_close(bars, horizon=3)
    aligned_target, _ = target.align(close, join="inner")
    aligned_target = aligned_target.dropna()
    target = aligned_target.rename("fwd_return")
    close = close.reindex(target.index)

    rng = np.random.default_rng(7)
    uniform = (target + pd.Series(rng.normal(0, 0.01, len(target)), index=target.index)).rename("uniform")

    regime_only = target.copy().rename("regime_only")
    split = len(target) // 6
    noise_tail = pd.Series(rng.normal(0, 1.0, len(target) - split), index=target.index[split:])
    regime_only.iloc[split:] = noise_tail

    uniform_eval = evaluate_feature(uniform, target, n_windows=6, close=close)
    regime_eval = evaluate_feature(regime_only, target, n_windows=6, close=close)

    assert uniform_eval.stability > regime_eval.stability


def test_regime_ics_has_bucket_keys_and_is_causal():
    bars = _synthetic_bars(250)
    target, close = _aligned_target_and_close(bars)
    feature = (target * 0.8 + 0.01).rename("feat")

    full = evaluate_feature(feature, target, regimes=3, close=close)
    assert set(full.regime_ics.keys()) == {"low", "mid", "high"}

    # Changing future close must not move an earlier bar's regime bucket.
    vol_full = compute_realized_vol(close.reindex(feature.index).sort_index(), 63).reindex(feature.index)
    buckets_full = assign_regime_buckets(vol_full, 3)

    bars_short = bars.iloc[:-20].copy()
    close_short = pd.Series(bars_short["close"].to_numpy(), index=bars_short["time"])
    vol_short = compute_realized_vol(close_short.reindex(feature.index[:-20]).sort_index(), 63).reindex(
        feature.index[:-20]
    )
    buckets_short = assign_regime_buckets(vol_short, 3)

    check_idx = 100
    assert buckets_full.iloc[check_idx] == buckets_short.iloc[check_idx]


def test_below_min_obs_returns_nan_metrics():
    bars = _synthetic_bars(80)
    target, close = _aligned_target_and_close(bars)
    feature = target.rename("feat")

    result = evaluate_feature(feature, target, close=close)
    assert result.n_obs < MIN_OBS
    assert np.isnan(result.ic)
    assert np.isnan(result.rank_ic)
    assert np.isnan(result.mutual_info)
    assert np.isnan(result.stability)


def test_evaluate_feature_is_deterministic():
    bars = _synthetic_bars(300)
    target, close = _aligned_target_and_close(bars)
    feature = (target + 0.01).rename("feat")

    first = evaluate_feature(feature, target, close=close)
    second = evaluate_feature(feature, target, close=close)
    assert first == second


def test_evaluate_matrix_produces_one_evaluation_per_column():
    bars = _synthetic_bars(300)
    target, close = _aligned_target_and_close(bars)
    times = bars["time"]
    frame = pd.DataFrame(
        {
            "feat.a": (target * 0.9).reindex(times).to_numpy(),
            "feat.b": (target * 0.5).reindex(times).to_numpy(),
        },
        index=times,
    )
    matrix = FeatureMatrix(
        matrix_id="test-matrix",
        frame=frame,
        manifest={
            "features": [
                {
                    "feature_id": "feat.a",
                    "name": "rsi",
                    "version": 1,
                    "params": {"period": 14},
                    "warmup_bars": 14,
                    "leakage_status": "clean",
                },
                {
                    "feature_id": "feat.b",
                    "name": "atr",
                    "version": 1,
                    "params": {"period": 14},
                    "warmup_bars": 14,
                    "leakage_status": "clean",
                },
            ]
        },
    )

    results = evaluate_matrix(matrix, target, close=close, bars=bars)
    assert len(results) == 2
    assert {item.feature_id for item in results} == {"feat.a", "feat.b"}
    assert all(item.leakage_status == "clean" for item in results)


def test_sample_real_feature_evaluation_vs_fwd_return():
    """Integration-style check for definition-of-done reporting."""
    bars = _synthetic_bars(300)
    target, close = _aligned_target_and_close(bars, horizon=5)
    from q_backend.features.compute import compute_feature
    from q_backend.features.registry import get_feature_spec

    spec = get_feature_spec("rsi")
    feature = compute_feature(bars, spec, {"period": 14}).series.rename("rsi.v1.test")

    result = evaluate_feature(feature, target, close=close)
    assert isinstance(result, FeatureEvaluation)
    assert result.target == "fwd_return"
    assert result.n_obs >= MIN_OBS
    assert not np.isnan(result.ic)
