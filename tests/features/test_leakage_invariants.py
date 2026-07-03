"""Neural feature sources under the causality invariant (WO181 Task 2).

Neural latents are registered per trained model, so they never appear in the static
``list_feature_specs()`` catalog that the parametrized feature-spec harness enumerates.
Before WO181 the only causality coverage for them was a silent ``spec.source == "neural"``
filter — i.e. none. Here we train a real model through the **PCA encoder path** (no torch
in CI), register its latents, and bring them under the same invariants used everywhere
else:

* ``test_neural_spec_is_causal`` — ``assert_causal`` with the ``train_end`` OOS-only
  semantics on each registered neural ``FeatureSpec``;
* ``test_latent_node_prefix_causality`` — the ``ind.latent`` genome node driven through a
  ``CompositeStrategy`` must be prefix-stable on OOS bars (this is the node-kind exempted
  from the generic genome harness because it needs a model);
* ``test_neural_oos_guard_catches_in_train_leak`` — an end-to-end proof that the neural
  OOS guard rejects a latent that becomes non-NaN at or before ``train_end``.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from sqlalchemy.orm import Session

from q_backend.backtesting.genome.composite_strategy import CompositeStrategy
from q_backend.features.compute import (
    FeatureSeries,
    clear_model_output_cache,
    compute_feature,
)
from q_backend.features.leakage import LeakageError, assert_causal, assert_neural_oos_only
from q_backend.features.registry import (
    get_feature_spec,
    register_neural_model_features,
    unregister_neural_model_features,
)
from q_backend.neural.training import default_train_encoder_config, train_encoder

_TRAIN_END_INDEX = 80
_N_BARS = 300


def _synthetic_bars(n: int = _N_BARS) -> pd.DataFrame:
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
        columns[name] = compute_feature(bars, get_feature_spec(name), {}).series.to_numpy()
    return pd.DataFrame(columns, index=bars["time"])


def _latent_entry_genome(*, latent_index: int = 0) -> dict:
    return {
        "version": 1,
        "genome_id": "latent-causality",
        "nodes": [
            {"id": "lat", "kind": "ind.latent", "params": {"latent_index": latent_index}, "inputs": []},
            {"id": "entry", "kind": "cmp.cross_above", "params": {"threshold": 0.0}, "inputs": ["lat"]},
            {"id": "exit", "kind": "cmp.cross_below", "params": {"threshold": 0.0}, "inputs": ["lat"]},
        ],
        "entry_long": {"ref": "entry"},
        "entry_short": {"ref": "exit"},
        "exit_long": {"ref": "exit"},
        "exit_short": {"ref": "entry"},
    }


@pytest.fixture
def neural_model(db_session: Session, lake_root_path):
    clear_model_output_cache()
    bars = _synthetic_bars()
    train_start = bars["time"].iloc[0].to_pydatetime()
    train_end = bars["time"].iloc[_TRAIN_END_INDEX].to_pydatetime()

    window = _classical_input_window(bars)
    train_mask = (window.index >= train_start) & (window.index <= train_end)
    config = default_train_encoder_config(
        symbol="SYN",
        timeframe="H1",
        train_start=train_start,
        train_end=train_end,
        n_latents=3,
        input_features=("rsi", "atr"),
        model_key="pca_leakage_invariant",
    )
    version = train_encoder(db_session, config, feature_window=window.loc[train_mask].dropna())
    keys = register_neural_model_features(version)
    yield version, keys, bars
    unregister_neural_model_features(keys)
    clear_model_output_cache()


def test_neural_spec_is_causal(neural_model) -> None:
    version, keys, bars = neural_model
    sample = [120, 180, 240, len(bars) - 1]
    for key in keys:
        spec = get_feature_spec(key)
        clear_model_output_cache()
        assert_causal(
            lambda frame, s=spec: compute_feature(frame, s, {}),
            bars,
            sample_indices=sample,
            spec=spec,
            train_end=version.train_end,
        )


def test_latent_node_prefix_causality(neural_model) -> None:
    version, _keys, bars = neural_model
    sample = [120, 180, 240, len(bars) - 1]

    def _compute(frame: pd.DataFrame) -> FeatureSeries:
        strategy = CompositeStrategy(
            genome=_latent_entry_genome(latent_index=0),
            params={},
            symbol="SYN",
            timeframe="H1",
            latent_model_hash=version.model_hash,
        )
        result = strategy.compute_indicators(frame)
        return FeatureSeries(
            feature_id="g_lat",
            series=result["g_lat"].reset_index(drop=True),
            warmup_bars=0,
            leakage_status="clean",
        )

    clear_model_output_cache()
    assert_causal(_compute, bars, sample_indices=sample)


def test_neural_oos_guard_catches_in_train_leak(neural_model) -> None:
    """End-to-end proof: a latent that is non-NaN at/before train_end is rejected."""
    version, keys, bars = neural_model
    spec = get_feature_spec(keys[0])
    clear_model_output_cache()
    result = compute_feature(bars, spec, {})

    times = bars["time"].reset_index(drop=True)
    leaked = result.series.reset_index(drop=True).copy()
    leaked.iloc[0] = 1.23456  # inject a value inside the training window (bar 0 <= train_end)

    with pytest.raises(LeakageError):
        assert_neural_oos_only(leaked, times, version.train_end)
