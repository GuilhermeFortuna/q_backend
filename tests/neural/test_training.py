"""Tests for neural encoder training service (WO142)."""

from __future__ import annotations

from datetime import datetime, timezone

import numpy as np
import pandas as pd
from sqlalchemy.orm import Session

from q_backend.neural.training import default_train_encoder_config, train_encoder
from q_backend.storage.lake.artifacts import read_neural_model


def test_train_encoder_writes_artifact_and_db_row(db_session: Session, lake_root_path) -> None:
    start = datetime(2024, 2, 1, tzinfo=timezone.utc)
    end = datetime(2024, 2, 15, tzinfo=timezone.utc)
    features = tuple(f"feature_{index:02d}" for index in range(1, 5))
    config = default_train_encoder_config(
        symbol="SYN",
        timeframe="H1",
        train_start=start,
        train_end=end,
        n_latents=2,
        input_features=features,
        model_key="pca_syn_h1",
    )

    index = pd.date_range(start, end, freq="h", tz="UTC")
    rng = np.random.default_rng(11)
    frame = pd.DataFrame(
        rng.normal(size=(len(index), len(features))),
        index=index,
        columns=features,
    )

    version = train_encoder(db_session, config, feature_window=frame)

    encoder = read_neural_model(version.model_hash)
    latents = encoder.transform(frame.tail(10))

    assert version.val_metrics
    assert "reconstruction_r2" in version.val_metrics
    assert "explained_variance" in version.val_metrics
    assert latents.shape[1] == 2
    assert list(latents.columns) == ["latent_001", "latent_002"]
