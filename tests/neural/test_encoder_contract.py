"""Shared encoder contract tests (WO142; parametrized in WO145)."""

from __future__ import annotations

from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

from q_backend.neural.encoder import EncoderConfig, NeuralEncoder, compute_model_id
from q_backend.neural.factory import create_encoder, load_encoder_from_artifact_payload
from q_backend.neural.pca_encoder import PCAEncoder
from q_backend.neural.torch_autoencoder import TorchAutoencoder
from q_backend.storage.lake.artifacts import read_neural_model, write_neural_model

_FAST_AE_HYPERPARAMS = {
    "lookback": 8,
    "hidden_dim": 16,
    "num_layers": 1,
    "epochs": 4,
    "batch_size": 16,
    "random_state": 0,
}


def _synthetic_feature_window(rows: int = 200, features: int = 6) -> pd.DataFrame:
    rng = np.random.default_rng(42)
    index = pd.date_range("2024-01-01", periods=rows, freq="h", tz="UTC")
    columns = [f"feature_{index:02d}" for index in range(1, features + 1)]
    values = rng.normal(size=(rows, features))
    return pd.DataFrame(values, index=index, columns=columns)


def _encoder_config(**overrides) -> EncoderConfig:
    kind = overrides.get("kind", "pca")
    hyperparams = overrides.pop("hyperparams", None)
    base = {
        "kind": kind,
        "symbol": "CCM$",
        "timeframe": "H1",
        "train_start": datetime(2024, 1, 1, tzinfo=timezone.utc),
        "train_end": datetime(2024, 1, 20, tzinfo=timezone.utc),
        "n_latents": 4,
        "input_features": tuple(f"feature_{index:02d}" for index in range(1, 7)),
        "hyperparams": {},
    }
    if kind == "autoencoder":
        base["hyperparams"] = dict(_FAST_AE_HYPERPARAMS)
    base.update(overrides)
    if hyperparams is not None:
        merged = dict(base["hyperparams"])
        merged.update(hyperparams)
        base["hyperparams"] = merged
    return EncoderConfig(**base)


def _encoder_factory(config: EncoderConfig):
    if config.kind == "pca":
        return PCAEncoder(config=config)
    if config.kind == "autoencoder":
        return TorchAutoencoder(config=config)
    raise ValueError(config.kind)


@pytest.mark.parametrize("kind", ["pca", "autoencoder"])
def test_encoder_satisfies_neural_encoder_protocol(kind: str) -> None:
    encoder = _encoder_factory(_encoder_config(kind=kind))
    assert isinstance(encoder, NeuralEncoder)


@pytest.mark.parametrize("kind", ["pca", "autoencoder"])
def test_encoder_contract_fit_transform_and_metrics(kind: str) -> None:
    window = _synthetic_feature_window()
    encoder = _encoder_factory(_encoder_config(kind=kind, n_latents=4))
    encoder.fit(window)
    latents = encoder.transform(window.tail(20))

    assert list(latents.columns) == encoder.latent_names
    assert latents.shape == (20, 4)
    assert latents.notna().any().any()
    assert encoder.val_metrics
    assert "reconstruction_r2" in encoder.val_metrics


@pytest.mark.parametrize("kind", ["pca", "autoencoder"])
def test_encoder_contract_model_id_stability(kind: str) -> None:
    window = _synthetic_feature_window()
    config = _encoder_config(kind=kind, n_latents=4)

    first = _encoder_factory(config)
    first.fit(window)
    second = _encoder_factory(config)
    second.fit(window)

    assert first.model_id == second.model_id
    assert first.model_id == compute_model_id(config)
    assert compute_model_id(_encoder_config(kind=kind, n_latents=5)) != first.model_id


@pytest.mark.parametrize(
    ("kind", "expected_cls"),
    [("pca", PCAEncoder), ("autoencoder", TorchAutoencoder)],
)
def test_create_encoder_dispatches_by_kind(kind: str, expected_cls: type) -> None:
    encoder = create_encoder(_encoder_config(kind=kind))
    assert isinstance(encoder, expected_cls)


@pytest.mark.parametrize("kind", ["pca", "autoencoder"])
def test_artifact_round_trip_is_kind_agnostic(kind: str, lake_root_path) -> None:
    window = _synthetic_feature_window()
    encoder = create_encoder(_encoder_config(kind=kind))
    encoder.fit(window)

    write_neural_model(encoder.model_id, encoder, model_key=f"{kind}_contract_h1")
    loaded = read_neural_model(encoder.model_id)

    assert isinstance(loaded, NeuralEncoder)
    pd.testing.assert_frame_equal(
        encoder.transform(window.tail(15)),
        loaded.transform(window.tail(15)),
    )

    payload = {
        "kind": encoder.config.kind,
        "config": encoder.config,
        "state": encoder.dump_artifact_state(),
    }
    reloaded = load_encoder_from_artifact_payload(payload)
    pd.testing.assert_frame_equal(
        loaded.transform(window.tail(15)),
        reloaded.transform(window.tail(15)),
    )
