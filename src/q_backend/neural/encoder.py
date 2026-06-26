"""Neural encoder protocol and configuration (WO142)."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

import pandas as pd


def _iso_z(dt: datetime) -> str:
    ts = pd.Timestamp(dt)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    else:
        ts = ts.tz_convert("UTC")
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class EncoderConfig:
    kind: str
    symbol: str
    timeframe: str
    train_start: datetime
    train_end: datetime
    n_latents: int
    input_features: tuple[str, ...]
    hyperparams: dict[str, Any] = field(default_factory=dict)


def canonical_config_payload(config: EncoderConfig) -> dict[str, Any]:
    return {
        "kind": config.kind,
        "symbol": config.symbol,
        "timeframe": config.timeframe.upper(),
        "train_start": _iso_z(config.train_start),
        "train_end": _iso_z(config.train_end),
        "n_latents": config.n_latents,
        "input_features": sorted(config.input_features),
        "hyperparams": {
            key: value
            for key, value in sorted(config.hyperparams.items())
        },
    }


def compute_model_id(config: EncoderConfig) -> str:
    """Content hash of canonicalized encoder config (mirrors ``matrix.compute_matrix_id``)."""
    canonical = json.dumps(
        canonical_config_payload(config),
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def latent_column_names(n_latents: int) -> list[str]:
    width = max(3, len(str(n_latents)))
    return [f"latent_{index:0{width}d}" for index in range(1, n_latents + 1)]


@runtime_checkable
class NeuralEncoder(Protocol):
    """Encoder contract shared by PCA, torch autoencoder, and future kinds.

    Registry, sync, and (WO143) feature-source consumers depend only on this
    surface — never on encoder-specific fields.
    """

    config: EncoderConfig
    model_id: str
    latent_names: list[str]

    @property
    def val_metrics(self) -> dict[str, float]: ...

    def fit(self, window: pd.DataFrame) -> None: ...

    def transform(self, window: pd.DataFrame) -> pd.DataFrame: ...

    def dump_artifact_state(self) -> dict[str, Any]: ...
