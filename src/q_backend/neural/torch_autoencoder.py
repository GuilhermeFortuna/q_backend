"""Temporal torch autoencoder implementing ``NeuralEncoder`` (WO145)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd
import torch
import torch.nn as nn
from sklearn.metrics import r2_score
from sklearn.preprocessing import StandardScaler

from q_backend.neural.encoder import (
    EncoderConfig,
    compute_model_id,
    latent_column_names,
)

_DEFAULT_HYPERPARAMS: dict[str, Any] = {
    "lookback": 16,
    "hidden_dim": 32,
    "num_layers": 1,
    "epochs": 25,
    "learning_rate": 1e-3,
    "mask_fraction": 0.15,
    "val_holdout_fraction": 0.2,
    "random_state": 0,
    "batch_size": 32,
}


def _set_deterministic(seed: int) -> torch.Generator:
    torch.manual_seed(seed)
    np.random.seed(seed)
    torch.use_deterministic_algorithms(True)
    if torch.cuda.is_available():  # pragma: no cover - CI is CPU-only
        torch.cuda.manual_seed_all(seed)
    torch.backends.cudnn.deterministic = True
    torch.backends.cudnn.benchmark = False
    generator = torch.Generator(device="cpu")
    generator.manual_seed(seed)
    return generator


def _serialize_state_dict(state_dict: dict[str, torch.Tensor]) -> dict[str, np.ndarray]:
    return {key: tensor.detach().cpu().numpy() for key, tensor in state_dict.items()}


def _deserialize_state_dict(
    serialized: dict[str, np.ndarray],
) -> dict[str, torch.Tensor]:
    return {key: torch.from_numpy(array) for key, array in serialized.items()}


def _build_sequences(values: np.ndarray, lookback: int) -> np.ndarray:
    if values.shape[0] < lookback:
        return np.empty((0, lookback, values.shape[1]), dtype=float)
    sequences = [values[index - lookback + 1 : index + 1] for index in range(lookback - 1, values.shape[0])]
    return np.stack(sequences, axis=0)


class _TemporalAutoencoderNet(nn.Module):
    """GRU encoder→bottleneck→GRU decoder for masked sequence reconstruction."""

    def __init__(
        self,
        *,
        n_features: int,
        lookback: int,
        n_latents: int,
        hidden_dim: int,
        num_layers: int,
    ) -> None:
        super().__init__()
        self.lookback = lookback
        self.n_latents = n_latents
        self.encoder_gru = nn.GRU(
            input_size=n_features,
            hidden_size=hidden_dim,
            num_layers=num_layers,
            batch_first=True,
        )
        self.to_latent = nn.Linear(hidden_dim, n_latents)
        self.from_latent = nn.Linear(n_latents, hidden_dim)
        self.decoder_gru = nn.GRU(
            input_size=hidden_dim,
            hidden_size=hidden_dim,
            num_layers=num_layers,
            batch_first=True,
        )
        self.output = nn.Linear(hidden_dim, n_features)
        self._num_layers = num_layers
        self._hidden_dim = hidden_dim

    def encode(self, sequences: torch.Tensor) -> torch.Tensor:
        _, hidden = self.encoder_gru(sequences)
        return self.to_latent(hidden[-1])

    def decode(self, latents: torch.Tensor, seq_len: int) -> torch.Tensor:
        hidden = self.from_latent(latents)
        repeated = hidden.unsqueeze(1).repeat(1, seq_len, 1)
        decoded, _ = self.decoder_gru(repeated)
        return self.output(decoded)

    def forward(self, sequences: torch.Tensor) -> torch.Tensor:
        latents = self.encode(sequences)
        return self.decode(latents, sequences.shape[1])


@dataclass
class TorchAutoencoder:
    config: EncoderConfig
    model_id: str = field(init=False)
    latent_names: list[str] = field(init=False)
    _scaler: StandardScaler | None = field(default=None, repr=False)
    _model: _TemporalAutoencoderNet | None = field(default=None, repr=False)
    _feature_columns: list[str] = field(default_factory=list, repr=False)
    _val_metrics: dict[str, float] = field(default_factory=dict, repr=False)
    _lookback: int = field(default=0, repr=False)

    def __post_init__(self) -> None:
        self.model_id = compute_model_id(self.config)
        self.latent_names = latent_column_names(self.config.n_latents)

    @property
    def val_metrics(self) -> dict[str, float]:
        return dict(self._val_metrics)

    def _resolved_hyperparams(self) -> dict[str, Any]:
        merged = dict(_DEFAULT_HYPERPARAMS)
        merged.update(self.config.hyperparams)
        return merged

    def _select_feature_columns(self, window: pd.DataFrame) -> list[str]:
        if list(window.columns) == list(self.config.input_features):
            return list(self.config.input_features)
        missing = [name for name in self.config.input_features if name not in window.columns]
        if missing:
            raise ValueError(f"Input features missing from window: {missing}")
        return list(self.config.input_features)

    def _split_fit_validation(self, values: np.ndarray, holdout_fraction: float) -> tuple[np.ndarray, np.ndarray]:
        if values.shape[0] < 2:
            return values, values[:0]
        holdout_rows = max(1, int(round(values.shape[0] * holdout_fraction)))
        if holdout_rows >= values.shape[0]:
            holdout_rows = max(1, values.shape[0] // 5)
        split_at = values.shape[0] - holdout_rows
        if split_at < 1:
            split_at = 1
        return values[:split_at], values[split_at:]

    def _build_model(self, n_features: int, hyperparams: dict[str, Any]) -> _TemporalAutoencoderNet:
        lookback = int(hyperparams["lookback"])
        return _TemporalAutoencoderNet(
            n_features=n_features,
            lookback=lookback,
            n_latents=self.config.n_latents,
            hidden_dim=int(hyperparams["hidden_dim"]),
            num_layers=int(hyperparams["num_layers"]),
        )

    def _masked_mse(
        self,
        predicted: torch.Tensor,
        target: torch.Tensor,
        mask: torch.Tensor,
    ) -> torch.Tensor:
        squared = (predicted - target) ** 2
        masked = squared * mask
        denom = mask.sum().clamp_min(1.0)
        return masked.sum() / denom

    def _make_mask(
        self,
        sequences: torch.Tensor,
        mask_fraction: float,
        generator: torch.Generator,
    ) -> torch.Tensor:
        mask = torch.rand(sequences.shape, generator=generator) < mask_fraction
        return mask.to(dtype=sequences.dtype)

    def _train_model(
        self,
        fit_sequences: np.ndarray,
        hyperparams: dict[str, Any],
    ) -> None:
        if fit_sequences.size == 0:
            raise ValueError("Not enough rows to build training sequences")

        seed = int(hyperparams["random_state"])
        generator = _set_deterministic(seed)
        device = torch.device("cpu")
        assert self._model is not None

        model = self._model.to(device)
        optimizer = torch.optim.Adam(
            model.parameters(),
            lr=float(hyperparams["learning_rate"]),
        )

        tensor = torch.from_numpy(fit_sequences.astype(np.float32))
        batch_size = int(hyperparams["batch_size"])
        epochs = int(hyperparams["epochs"])
        mask_fraction = float(hyperparams["mask_fraction"])

        for _epoch in range(epochs):
            model.train()
            for start in range(0, tensor.shape[0], batch_size):
                batch = tensor[start : start + batch_size].to(device)
                mask = self._make_mask(batch, mask_fraction, generator).to(device)
                masked_input = batch * (1.0 - mask)
                optimizer.zero_grad(set_to_none=True)
                reconstructed = model(masked_input)
                loss = self._masked_mse(reconstructed, batch, mask)
                loss.backward()
                optimizer.step()

        model.eval()

    def _compute_validation_metrics(
        self,
        val_sequences: np.ndarray,
    ) -> dict[str, float]:
        if val_sequences.size == 0 or self._model is None:
            return {"reconstruction_r2": 0.0, "reconstruction_mse": 0.0}

        device = torch.device("cpu")
        model = self._model.to(device)
        model.eval()
        with torch.no_grad():
            batch = torch.from_numpy(val_sequences.astype(np.float32)).to(device)
            reconstructed = model(batch).cpu().numpy()

        flat_target = batch.cpu().numpy().reshape(-1)
        flat_pred = reconstructed.reshape(-1)
        mse = float(np.mean((flat_target - flat_pred) ** 2))
        r2 = float(r2_score(flat_target, flat_pred))
        return {"reconstruction_r2": r2, "reconstruction_mse": mse}

    def fit(self, window: pd.DataFrame) -> None:
        if window.empty:
            raise ValueError("Training window must not be empty")

        hyperparams = self._resolved_hyperparams()
        lookback = int(hyperparams["lookback"])
        self._lookback = lookback
        self._feature_columns = self._select_feature_columns(window)
        # Drop leading warm-up NaNs (and any gap rows) so the model never trains on
        # NaN — which would silently produce NaN weights/latents, not an error.
        complete = window[self._feature_columns].astype(float).dropna(axis=0, how="any")
        if complete.empty:
            raise ValueError("Training window has no rows without missing feature values")
        raw = complete.to_numpy()

        fit_values, val_values = self._split_fit_validation(
            raw,
            float(hyperparams["val_holdout_fraction"]),
        )
        if fit_values.shape[0] < lookback:
            raise ValueError(f"Training window needs at least {lookback} rows for lookback={lookback}.")

        scaler = StandardScaler()
        scaled_fit = scaler.fit_transform(fit_values)
        scaled_val = scaler.transform(val_values) if val_values.size else val_values.reshape(0, raw.shape[1])

        fit_sequences = _build_sequences(scaled_fit, lookback)
        val_sequences = _build_sequences(scaled_val, lookback)

        self._scaler = scaler
        self._model = self._build_model(scaled_fit.shape[1], hyperparams)
        self._train_model(fit_sequences, hyperparams)
        self._val_metrics = self._compute_validation_metrics(val_sequences)

    def _encode_sequences(self, sequences: np.ndarray) -> np.ndarray:
        if self._model is None:
            raise RuntimeError("TorchAutoencoder must be fit before transform")
        if sequences.size == 0:
            return np.empty((0, self.config.n_latents), dtype=float)

        device = torch.device("cpu")
        model = self._model.to(device)
        model.eval()
        encoded_batches: list[np.ndarray] = []
        batch_size = 64
        with torch.no_grad():
            for start in range(0, sequences.shape[0], batch_size):
                batch = torch.from_numpy(sequences[start : start + batch_size].astype(np.float32)).to(device)
                latents = model.encode(batch).cpu().numpy()
                encoded_batches.append(latents)
        return np.concatenate(encoded_batches, axis=0)

    def transform(self, window: pd.DataFrame) -> pd.DataFrame:
        if self._scaler is None or self._model is None:
            raise RuntimeError("TorchAutoencoder must be fit before transform")

        if window.empty:
            return pd.DataFrame(columns=self.latent_names, index=window.index)

        feature_columns = self._select_feature_columns(window)
        scaled = self._scaler.transform(window[feature_columns].astype(float).to_numpy())
        lookback = self._lookback
        sequences = _build_sequences(scaled, lookback)

        encoded = self._encode_sequences(sequences)
        latent_values = np.full((scaled.shape[0], self.config.n_latents), np.nan, dtype=float)
        if encoded.size:
            latent_values[lookback - 1 :] = encoded[:, : self.config.n_latents]
            if encoded.shape[1] < self.config.n_latents:
                latent_values[lookback - 1 :, encoded.shape[1] :] = np.nan

        latent_frame = pd.DataFrame(
            latent_values,
            index=window.index,
            columns=self.latent_names,
        )
        return latent_frame[self.latent_names]

    def dump_artifact_state(self) -> dict[str, Any]:
        if self._model is None:
            raise RuntimeError("TorchAutoencoder must be fit before dump_artifact_state")
        return {
            "scaler": self._scaler,
            "model_state": _serialize_state_dict(self._model.state_dict()),
            "feature_columns": list(self._feature_columns),
            "val_metrics": dict(self._val_metrics),
            "lookback": self._lookback,
            "architecture": {
                "hidden_dim": self._model._hidden_dim,
                "num_layers": self._model._num_layers,
            },
        }

    @classmethod
    def load_from_artifact_state(cls, config: EncoderConfig, state: dict[str, Any]) -> TorchAutoencoder:
        encoder = cls(config=config)
        encoder._scaler = state.get("scaler")
        encoder._feature_columns = list(state.get("feature_columns", []))
        encoder._val_metrics = dict(state.get("val_metrics", {}))
        encoder._lookback = int(state.get("lookback", _DEFAULT_HYPERPARAMS["lookback"]))

        architecture = state.get("architecture", {})
        hyperparams = {
            **_DEFAULT_HYPERPARAMS,
            **config.hyperparams,
            "hidden_dim": architecture.get(
                "hidden_dim", config.hyperparams.get("hidden_dim", _DEFAULT_HYPERPARAMS["hidden_dim"])
            ),
            "num_layers": architecture.get(
                "num_layers",
                config.hyperparams.get("num_layers", _DEFAULT_HYPERPARAMS["num_layers"]),
            ),
            "lookback": encoder._lookback,
        }
        n_features = len(encoder._feature_columns) or len(config.input_features)
        encoder._model = encoder._build_model(n_features, hyperparams)
        model_state = state.get("model_state")
        if model_state is not None:
            encoder._model.load_state_dict(_deserialize_state_dict(model_state))
        encoder._model.eval()
        return encoder
