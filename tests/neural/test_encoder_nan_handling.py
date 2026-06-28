"""Encoders must tolerate the warm-up NaNs inherent in classical feature windows.

Classical features (RSI/MACD/Bollinger/...) emit leading NaNs until their lookback
fills. The encoders fit on those windows, so they must drop incomplete rows for
fitting and emit NaN latents for incomplete rows on transform — never feed NaN to
sklearn PCA (raises) or torch (trains on garbage).
"""

from __future__ import annotations

import math
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from q_backend.neural.encoder import EncoderConfig
from q_backend.neural.pca_encoder import PCAEncoder
from q_backend.neural.torch_autoencoder import TorchAutoencoder

_WARMUP = 8


def _window_with_warmup(rows: int = 80, n_features: int = 4) -> pd.DataFrame:
    rng = np.random.default_rng(0)
    index = pd.date_range("2024-01-01", periods=rows, freq="h", tz="UTC")
    columns = [f"feature_{i:02d}" for i in range(n_features)]
    frame = pd.DataFrame(rng.normal(size=(rows, n_features)), index=index, columns=columns)
    frame.iloc[:_WARMUP] = np.nan  # leading warm-up region, like real feature lookback
    return frame


def _config(columns: list[str], *, kind: str, n_latents: int = 2, **hyperparams) -> EncoderConfig:
    return EncoderConfig(
        kind=kind,
        symbol="SYN",
        timeframe="H1",
        train_start=datetime(2024, 1, 1, tzinfo=timezone.utc),
        train_end=datetime(2024, 1, 5, tzinfo=timezone.utc),
        n_latents=n_latents,
        input_features=tuple(columns),
        hyperparams=hyperparams,
    )


def test_pca_encoder_tolerates_warmup_nans() -> None:
    window = _window_with_warmup()
    encoder = PCAEncoder(config=_config(list(window.columns), kind="pca"))

    encoder.fit(window)  # must not raise "Input X contains NaN"
    latents = encoder.transform(window)

    assert latents.iloc[:_WARMUP].isna().all().all()
    assert latents.iloc[_WARMUP:].notna().any().any()
    assert math.isfinite(encoder.val_metrics["reconstruction_r2"])


def test_torch_autoencoder_tolerates_warmup_nans() -> None:
    window = _window_with_warmup()
    lookback = 4
    encoder = TorchAutoencoder(
        config=_config(
            list(window.columns),
            kind="autoencoder",
            lookback=lookback,
            hidden_dim=8,
            num_layers=1,
            epochs=2,
            batch_size=16,
            random_state=0,
        )
    )

    encoder.fit(window)  # must not train on NaN
    latents = encoder.transform(window)

    # Real (finite) latents appear once both warm-up and lookback are satisfied.
    assert latents.iloc[_WARMUP + lookback :].notna().any().any()
    assert math.isfinite(encoder.val_metrics["reconstruction_r2"])
