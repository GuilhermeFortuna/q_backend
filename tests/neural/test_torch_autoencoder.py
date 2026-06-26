"""Tests for the temporal torch autoencoder (WO145)."""

from __future__ import annotations

from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from q_backend.features.compute import clear_model_output_cache, compute_feature
from q_backend.features.leakage import assert_neural_oos_only
from q_backend.features.registry import get_feature_spec, neural_catalog_key, unregister_neural_model_features
from q_backend.neural.encoder import compute_model_id
from q_backend.neural.factory import create_encoder, load_encoder_from_artifact_payload
from q_backend.neural.torch_autoencoder import TorchAutoencoder
from q_backend.neural.training import default_train_encoder_config, train_encoder
from q_backend.storage.db.base import Base
from q_backend.storage.lake.artifacts import read_neural_model, write_neural_model
from q_backend.storage.settings import get_settings

_FAST_HYPERPARAMS = {
    "lookback": 8,
    "hidden_dim": 16,
    "num_layers": 1,
    "epochs": 4,
    "batch_size": 16,
    "random_state": 0,
}


def _synthetic_feature_window(rows: int = 120, features: int = 6) -> pd.DataFrame:
    rng = np.random.default_rng(42)
    index = pd.date_range("2024-01-01", periods=rows, freq="h", tz="UTC")
    columns = [f"feature_{index:02d}" for index in range(1, features + 1)]
    values = rng.normal(size=(rows, features))
    return pd.DataFrame(values, index=index, columns=columns)


def _encoder_config(**overrides) -> object:
    base = {
        "kind": "autoencoder",
        "symbol": "SYN",
        "timeframe": "H1",
        "train_start": datetime(2024, 1, 1, tzinfo=timezone.utc),
        "train_end": datetime(2024, 1, 10, tzinfo=timezone.utc),
        "n_latents": 3,
        "input_features": tuple(f"feature_{index:02d}" for index in range(1, 7)),
        "hyperparams": dict(_FAST_HYPERPARAMS),
    }
    base.update(overrides)
    if "hyperparams" in overrides:
        merged = dict(_FAST_HYPERPARAMS)
        merged.update(overrides["hyperparams"])
        base["hyperparams"] = merged
    from q_backend.neural.encoder import EncoderConfig

    return EncoderConfig(**base)


@pytest.fixture
def lake_root_path(tmp_path, monkeypatch):
    monkeypatch.setenv("Q_DATA_LAKE_ROOT", str(tmp_path))
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


def test_torch_autoencoder_fit_transform_and_metrics() -> None:
    window = _synthetic_feature_window()
    encoder = TorchAutoencoder(config=_encoder_config())
    encoder.fit(window)
    latents = encoder.transform(window.tail(30))

    assert list(latents.columns) == encoder.latent_names
    assert latents.shape == (30, 3)
    assert latents.iloc[_FAST_HYPERPARAMS["lookback"] - 1 :].notna().any().any()
    assert latents.iloc[: _FAST_HYPERPARAMS["lookback"] - 1].isna().all().all()
    assert "reconstruction_r2" in encoder.val_metrics
    assert "reconstruction_mse" in encoder.val_metrics


def test_torch_autoencoder_is_deterministic() -> None:
    window = _synthetic_feature_window()
    config = _encoder_config()

    first = TorchAutoencoder(config=config)
    first.fit(window)
    second = TorchAutoencoder(config=config)
    second.fit(window)

    assert first.model_id == second.model_id
    assert first.model_id == compute_model_id(config)
    pd.testing.assert_frame_equal(
        first.transform(window.tail(25)),
        second.transform(window.tail(25)),
    )


def test_create_encoder_dispatches_autoencoder_kind() -> None:
    encoder = create_encoder(_encoder_config())
    assert isinstance(encoder, TorchAutoencoder)


def test_torch_autoencoder_artifact_round_trip(lake_root_path) -> None:
    window = _synthetic_feature_window()
    encoder = create_encoder(_encoder_config())
    encoder.fit(window)

    write_neural_model(encoder.model_id, encoder, model_key="ae_contract_h1")
    loaded = read_neural_model(encoder.model_id)

    pd.testing.assert_frame_equal(
        encoder.transform(window.tail(20)),
        loaded.transform(window.tail(20)),
    )

    payload = {
        "kind": encoder.config.kind,
        "config": encoder.config,
        "state": encoder.dump_artifact_state(),
    }
    reloaded = load_encoder_from_artifact_payload(payload)
    pd.testing.assert_frame_equal(
        loaded.transform(window.tail(20)),
        reloaded.transform(window.tail(20)),
    )


def test_torch_autoencoder_oos_contract_via_compute_path(
    lake_root_path,
    monkeypatch,
) -> None:
    from q_backend.market_data.models import OHLCV

    rng = np.random.default_rng(7)
    n = 120
    steps = rng.normal(0.0, 1.0, size=n)
    close = 100.0 + np.cumsum(steps)
    times = pd.date_range("2023-01-01", periods=n, freq="h", tz="UTC")
    bars = pd.DataFrame(
        {
            "time": times,
            "open": close,
            "high": close + 0.5,
            "low": close - 0.5,
            "close": close,
            "volume": rng.integers(1000, 2000, size=n).astype(float),
        }
    )
    feature_window = pd.DataFrame(
        {
            "feature_01": rng.normal(size=n),
            "feature_02": rng.normal(size=n),
        },
        index=times,
    )

    train_start = datetime(2023, 1, 1, tzinfo=timezone.utc)
    train_end = datetime(2023, 1, 5, tzinfo=timezone.utc)
    train_mask = (feature_window.index >= train_start) & (feature_window.index <= train_end)

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    config = default_train_encoder_config(
        kind="autoencoder",
        symbol="SYN",
        timeframe="H1",
        train_start=train_start,
        train_end=train_end,
        n_latents=2,
        input_features=("feature_01", "feature_02"),
        model_key="ae_oos_test",
        hyperparams=_FAST_HYPERPARAMS,
    )
    version = train_encoder(
        session,
        config,
        feature_window=feature_window.loc[train_mask],
    )
    key = neural_catalog_key(version.latent_names[0], version.model_hash)
    clear_model_output_cache()

    ohlcv = [
        OHLCV(
            time=row.time.to_pydatetime(),
            open=row.open,
            high=row.high,
            low=row.low,
            close=row.close,
            tick_volume=int(row.volume),
        )
        for row in bars.itertuples()
    ]

    def _read_ohlcv(symbol, timeframe, start_dt, end_dt):
        return [bar for bar in ohlcv if start_dt <= bar.time <= end_dt]

    import q_backend.features.compute as compute_mod

    monkeypatch.setattr("q_backend.features.matrix.read_ohlcv", _read_ohlcv)

    def _stub_input_window(df, input_features):
        out = feature_window.reindex(df["time"])
        out.index = df["time"]
        return out

    monkeypatch.setattr(compute_mod, "_build_classical_input_window", _stub_input_window)

    try:
        spec = get_feature_spec(key)
        result = compute_feature(bars, spec, {})
        train_end_ts = pd.Timestamp(version.train_end)
        if train_end_ts.tzinfo is None:
            train_end_ts = train_end_ts.tz_localize("UTC")
        assert result.series.loc[result.series.index <= train_end_ts].isna().all()
        assert result.series.loc[result.series.index > train_end_ts].notna().any()
        assert_neural_oos_only(
            result.series.reset_index(drop=True),
            bars["time"],
            version.train_end,
        )
    finally:
        unregister_neural_model_features(
            [neural_catalog_key(name, version.model_hash) for name in version.latent_names]
        )
