"""Tests for neural FeatureSpec registration (WO143)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy.orm import Session

from q_backend.backtesting.genome.node_specs import NODE_SPECS
from q_backend.features.registry import (
    assert_catalog_consistent,
    feature_id,
    get_feature_spec,
    list_feature_specs,
    neural_catalog_key,
    register_neural_model_features,
    unregister_neural_model_features,
)
from q_backend.neural.training import default_train_encoder_config, train_encoder


def _classical_feature_window(
    rows: int,
    *,
    start: datetime,
    end: datetime,
) -> tuple[object, list[str]]:
    import numpy as np
    import pandas as pd

    from q_backend.features.compute import compute_feature
    from q_backend.features.registry import get_feature_spec

    rng = np.random.default_rng(7)
    steps = rng.normal(0.0, 1.0, size=rows)
    close = 100.0 + np.cumsum(steps)
    open_ = close - rng.normal(0.0, 0.5, size=rows)
    high = np.maximum(open_, close) + np.abs(rng.normal(0.0, 0.5, size=rows))
    low = np.minimum(open_, close) - np.abs(rng.normal(0.0, 0.5, size=rows))
    volume = rng.integers(1_000, 5_000, size=rows).astype(float)
    times = pd.date_range(start, end, periods=rows, tz="UTC")
    bars = pd.DataFrame(
        {
            "time": times,
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
        }
    )

    input_features = ("rsi", "atr")
    columns = {}
    for name in input_features:
        spec = get_feature_spec(name)
        columns[name] = compute_feature(bars, spec, {}).series.to_numpy()
    window = pd.DataFrame(columns, index=times)
    return window, list(input_features)


def _train_version(
    db_session: Session,
    lake_root_path,
    *,
    n_latents: int = 3,
    model_key: str = "pca_test_h1",
    train_end: datetime | None = None,
) -> tuple[object, list[str]]:
    train_start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    resolved_train_end = train_end or datetime(2024, 1, 10, tzinfo=timezone.utc)
    window, input_features = _classical_feature_window(
        240, start=train_start, end=datetime(2024, 1, 20, tzinfo=timezone.utc)
    )
    train_mask = (window.index >= train_start) & (window.index <= resolved_train_end)
    config = default_train_encoder_config(
        symbol="SYN",
        timeframe="H1",
        train_start=train_start,
        train_end=resolved_train_end,
        n_latents=n_latents,
        input_features=tuple(input_features),
        model_key=model_key,
    )
    version = train_encoder(
        db_session,
        config,
        feature_window=window.loc[train_mask].dropna(),
    )
    keys = register_neural_model_features(version)
    return version, keys


@pytest.fixture
def registered_neural_specs(db_session, lake_root_path):
    version, keys = _train_version(db_session, lake_root_path)
    yield version, keys
    unregister_neural_model_features(keys)


def test_register_neural_model_features_adds_specs(
    registered_neural_specs,
) -> None:
    version, keys = registered_neural_specs
    assert len(keys) == version.n_latents
    assert_catalog_consistent()
    for key in keys:
        spec = get_feature_spec(key)
        assert spec.source == "neural"
        assert spec.model_hash == version.model_hash
        assert spec.forward_window == 0
        assert spec.latent_index is not None


def test_classical_specs_unchanged_after_neural_registration(
    registered_neural_specs,
) -> None:
    _version, keys = registered_neural_specs
    classical = [
        spec
        for spec in list_feature_specs()
        if spec.source == "classical" and spec.name not in {k.split("@")[0] for k in keys}
    ]
    for spec in classical:
        assert spec.param_keys == NODE_SPECS[spec.node_kind].allowed_param_keys
        assert spec.forward_window == 0


def test_feature_id_differs_across_model_hashes(
    db_session,
    lake_root_path,
) -> None:
    version_a, keys_a = _train_version(db_session, lake_root_path, n_latents=3, model_key="pca_a")
    version_b, keys_b = _train_version(
        db_session,
        lake_root_path,
        n_latents=3,
        model_key="pca_b",
        train_end=datetime(2024, 1, 12, tzinfo=timezone.utc),
    )
    try:
        spec_a = get_feature_spec(keys_a[0])
        spec_b = get_feature_spec(neural_catalog_key(version_b.latent_names[0], version_b.model_hash))
        assert spec_a.latent_index == spec_b.latent_index
        assert feature_id(spec_a, {}) != feature_id(spec_b, {})
    finally:
        unregister_neural_model_features(keys_a + keys_b)
