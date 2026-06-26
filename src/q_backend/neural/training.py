"""Neural encoder training service (WO142)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import pandas as pd

from q_backend.features.registry import register_neural_model_features
from q_backend.neural.encoder import EncoderConfig
from q_backend.neural.factory import create_encoder
from q_backend.storage.db.models import NeuralModelStatus, NeuralModelVersion
from q_backend.storage.db.repositories import (
    create_neural_model,
    create_neural_model_version,
)
from q_backend.storage.lake.artifacts import write_neural_model
from sqlalchemy.orm import Session

WindowBuilder = Callable[[str, str, datetime, datetime], pd.DataFrame]


@dataclass(frozen=True)
class TrainEncoderConfig:
    model_key: str
    encoder_config: EncoderConfig


def _default_model_key(symbol: str, timeframe: str, kind: str) -> str:
    symbol_key = symbol.lower().replace("$", "").replace("/", "_")
    return f"{kind}_{symbol_key}_{timeframe.lower()}"


def train_encoder(
    session: Session,
    config: TrainEncoderConfig,
    *,
    feature_window: pd.DataFrame | None = None,
    window_builder: WindowBuilder | None = None,
) -> NeuralModelVersion:
    encoder_config = config.encoder_config
    if feature_window is None:
        if window_builder is None:
            raise ValueError("feature_window or window_builder is required")
        feature_window = window_builder(
            encoder_config.symbol,
            encoder_config.timeframe,
            encoder_config.train_start,
            encoder_config.train_end,
        )

    if feature_window.empty:
        raise ValueError("Feature window is empty for the requested train range")

    encoder = create_encoder(encoder_config)
    encoder.fit(feature_window)
    artifact_paths = write_neural_model(
        encoder.model_id,
        encoder,
        model_key=config.model_key,
    )

    model = create_neural_model(
        session,
        model_key=config.model_key,
        kind=encoder_config.kind,
        symbol=encoder_config.symbol,
        timeframe=encoder_config.timeframe,
    )
    version = create_neural_model_version(
        session,
        model_id=model.id,
        model_hash=encoder.model_id,
        status=NeuralModelStatus.TRAINED.value,
        train_start=encoder_config.train_start,
        train_end=encoder_config.train_end,
        n_latents=encoder_config.n_latents,
        input_features=list(encoder_config.input_features),
        hyperparams=dict(encoder_config.hyperparams),
        val_metrics=encoder.val_metrics,
        latent_names=encoder.latent_names,
        artifact_path=artifact_paths["encoder"],
    )
    register_neural_model_features(version)
    return version


def default_train_encoder_config(
    *,
    kind: str = "pca",
    symbol: str,
    timeframe: str,
    train_start: datetime,
    train_end: datetime,
    n_latents: int,
    input_features: tuple[str, ...],
    model_key: str | None = None,
    hyperparams: dict[str, Any] | None = None,
) -> TrainEncoderConfig:
    encoder_config = EncoderConfig(
        kind=kind,
        symbol=symbol,
        timeframe=timeframe,
        train_start=train_start,
        train_end=train_end,
        n_latents=n_latents,
        input_features=input_features,
        hyperparams=hyperparams or {},
    )
    return TrainEncoderConfig(
        model_key=model_key or _default_model_key(symbol, timeframe, kind),
        encoder_config=encoder_config,
    )
