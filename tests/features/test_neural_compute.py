"""Tests for PIT-safe neural feature computation (WO143)."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest
from sqlalchemy.orm import Session

from q_backend.features.compute import clear_model_output_cache, compute_feature
from q_backend.features.leakage import assert_causal
from q_backend.features.matrix import FeatureRequest, build_feature_matrix
from q_backend.features.registry import (
    get_feature_spec,
    register_neural_model_features,
    unregister_neural_model_features,
)
from q_backend.neural.training import default_train_encoder_config, train_encoder
from q_backend.storage.lake.artifacts import read_neural_model

# SHA-256 of ``series.to_numpy().tobytes()`` for seed=42 synthetic bars (pre-WO143 baseline).
_CLASSICAL_SERIES_SHA256 = {
    "rsi": "56f3324d07c49075de571f8e6e74712c177105d4ada2290b8bcdce86c16c39ff",
    "atr": "2e4e17e17cd4d344f8b0f93b33d4e4e6075ccde4252bdf5f3f0f38311c8ab28e",
}


def _series_sha256(series: pd.Series) -> str:
    return hashlib.sha256(series.to_numpy().tobytes()).hexdigest()


def _synthetic_bars(n: int = 260) -> pd.DataFrame:
    rng = np.random.default_rng(42)
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


def _classical_input_window(bars: pd.DataFrame) -> pd.DataFrame:
    columns = {}
    for name in ("rsi", "atr"):
        spec = get_feature_spec(name)
        columns[name] = compute_feature(bars, spec, {}).series.to_numpy()
    return pd.DataFrame(columns, index=bars["time"])


def _train_and_register(
    db_session: Session,
    lake_root_path,
    *,
    n_latents: int = 3,
) -> tuple[object, list[str], pd.DataFrame]:
    bars = _synthetic_bars(300)
    train_start = pd.Timestamp("2023-01-05", tz="UTC").to_pydatetime()
    train_end = pd.Timestamp("2023-01-05", tz="UTC").to_pydatetime()

    full_window = _classical_input_window(bars)
    train_mask = (full_window.index >= train_start) & (full_window.index <= train_end)

    config = default_train_encoder_config(
        symbol="SYN",
        timeframe="H1",
        train_start=train_start,
        train_end=train_end,
        n_latents=n_latents,
        input_features=("rsi", "atr"),
        model_key="pca_compute_test",
    )
    version = train_encoder(
        db_session,
        config,
        feature_window=full_window.loc[train_mask].dropna(),
    )
    keys = register_neural_model_features(version)
    return version, keys, bars


@pytest.fixture
def neural_setup(db_session, lake_root_path):
    clear_model_output_cache()
    version, keys, bars = _train_and_register(db_session, lake_root_path)
    yield version, keys, bars
    unregister_neural_model_features(keys)
    clear_model_output_cache()


def test_neural_latents_nan_inside_train_window(neural_setup) -> None:
    version, keys, bars = neural_setup
    train_end = pd.Timestamp(version.train_end)
    if train_end.tzinfo is None:
        train_end = train_end.tz_localize("UTC")

    spec = get_feature_spec(keys[0])
    result = compute_feature(bars, spec, {})

    in_train = result.series.index <= train_end
    assert result.series.loc[in_train].isna().all()
    assert result.series.loc[~in_train].notna().any()
    assert result.warmup_bars == int((result.series.index <= train_end).sum())


def test_neural_leakage_status_oos_vs_overlapping(neural_setup) -> None:
    version, keys, bars = neural_setup
    train_end = pd.Timestamp(version.train_end)
    if train_end.tzinfo is None:
        train_end = train_end.tz_localize("UTC")
    spec = get_feature_spec(keys[0])

    oos_mask = bars["time"] > train_end
    oos_bars = bars.loc[oos_mask].reset_index(drop=True)
    oos_result = compute_feature(oos_bars, spec, {})
    assert oos_result.leakage_status == "clean"

    overlap_result = compute_feature(bars, spec, {})
    assert overlap_result.leakage_status == "suspect"


def test_model_output_cache_single_transform(neural_setup, monkeypatch) -> None:
    version, keys, bars = neural_setup
    encoder = read_neural_model(version.model_hash)
    call_count = {"n": 0}
    original_transform = encoder.transform

    def counting_transform(window: pd.DataFrame) -> pd.DataFrame:
        call_count["n"] += 1
        return original_transform(window)

    clear_model_output_cache()

    with patch(
        "q_backend.features.compute.read_neural_model",
        return_value=encoder,
    ):
        encoder.transform = counting_transform  # type: ignore[method-assign]
        for key in keys:
            compute_feature(bars, get_feature_spec(key), {})

    assert call_count["n"] == 1


def test_build_feature_matrix_neural_columns(neural_setup, monkeypatch) -> None:
    version, keys, bars = neural_setup
    clear_model_output_cache()

    start = bars["time"].iloc[0].to_pydatetime()
    end = bars["time"].iloc[-1].to_pydatetime()

    # build_feature_matrix reads OHLCV from lake; stub with our synthetic bars.
    monkeypatch.setattr(
        "q_backend.features.matrix.read_ohlcv_fresh",
        lambda symbol, timeframe, s, e: [],
    )
    monkeypatch.setattr(
        "q_backend.features.matrix._ohlcv_to_compute_bars",
        lambda _: bars,
    )
    monkeypatch.setattr(
        "q_backend.features.matrix.feature_matrix_exists",
        lambda _: False,
    )
    monkeypatch.setattr(
        "q_backend.features.matrix.write_feature_matrix",
        lambda *args, **kwargs: None,
    )

    requests = [
        FeatureRequest(name=key, version=None, params={}) for key in keys
    ]
    matrix = build_feature_matrix("SYN", "H1", start, end, requests, use_cache=False)
    assert matrix.frame.shape[1] == len(keys)


def test_classical_compute_regression_unchanged() -> None:
    bars = _synthetic_bars()
    for name, params in [("rsi", {"period": 14}), ("atr", {"period": 14})]:
        spec = get_feature_spec(name)
        result = compute_feature(bars, spec, params)
        assert _series_sha256(result.series) == _CLASSICAL_SERIES_SHA256[name]


def test_assert_causal_neural_branch(neural_setup) -> None:
    version, keys, bars = neural_setup
    spec = get_feature_spec(keys[0])
    train_end = version.train_end

    def _compute(frame: pd.DataFrame):
        return compute_feature(frame, spec, {})

    assert_causal(
        _compute,
        bars,
        sample_indices=[200, 250, len(bars) - 1],
        spec=spec,
        train_end=train_end,
    )
