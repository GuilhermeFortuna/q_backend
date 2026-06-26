"""Tests for the PCA reference encoder (WO142)."""

from __future__ import annotations

from datetime import datetime, timezone

import numpy as np
import pandas as pd

from q_backend.neural.encoder import EncoderConfig, compute_model_id
from q_backend.neural.pca_encoder import PCAEncoder


def _synthetic_feature_window(rows: int = 200, features: int = 6) -> pd.DataFrame:
    rng = np.random.default_rng(42)
    index = pd.date_range("2024-01-01", periods=rows, freq="h", tz="UTC")
    columns = [f"feature_{index:02d}" for index in range(1, features + 1)]
    values = rng.normal(size=(rows, features))
    return pd.DataFrame(values, index=index, columns=columns)


def _encoder_config(**overrides) -> EncoderConfig:
    base = EncoderConfig(
        kind="pca",
        symbol="CCM$",
        timeframe="H1",
        train_start=datetime(2024, 1, 1, tzinfo=timezone.utc),
        train_end=datetime(2024, 1, 20, tzinfo=timezone.utc),
        n_latents=4,
        input_features=tuple(f"feature_{index:02d}" for index in range(1, 7)),
        hyperparams={},
    )
    return EncoderConfig(**{**base.__dict__, **overrides})


def test_fit_transform_returns_named_latent_columns() -> None:
    window = _synthetic_feature_window()
    encoder = PCAEncoder(config=_encoder_config(n_latents=4))
    encoder.fit(window)
    latents = encoder.transform(window.tail(20))

    assert list(latents.columns) == ["latent_001", "latent_002", "latent_003", "latent_004"]
    assert latents.shape == (20, 4)
    assert np.isfinite(latents.to_numpy()).all()


def test_two_fits_are_deterministic() -> None:
    window = _synthetic_feature_window()
    config = _encoder_config(n_latents=4)

    first = PCAEncoder(config=config)
    first.fit(window)
    first_latents = first.transform(window.tail(25))

    second = PCAEncoder(config=config)
    second.fit(window)
    second_latents = second.transform(window.tail(25))

    pd.testing.assert_frame_equal(first_latents, second_latents)


def test_model_id_is_stable_and_changes_with_hyperparams() -> None:
    window = _synthetic_feature_window()
    base_config = _encoder_config(n_latents=4)

    first = PCAEncoder(config=base_config)
    first.fit(window)
    second = PCAEncoder(config=base_config)
    second.fit(window)

    assert first.model_id == second.model_id
    assert first.model_id == compute_model_id(base_config)

    changed = _encoder_config(n_latents=5)
    assert compute_model_id(changed) != first.model_id
