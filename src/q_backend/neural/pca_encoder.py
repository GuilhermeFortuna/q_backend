"""PCA reference encoder using sklearn (WO142)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd
from sklearn.decomposition import PCA
from sklearn.metrics import r2_score
from sklearn.preprocessing import StandardScaler

from q_backend.neural.encoder import (
    EncoderConfig,
    compute_model_id,
    latent_column_names,
)

_DEFAULT_HYPERPARAMS: dict[str, Any] = {
    "svd_solver": "full",
    "random_state": 0,
    "val_holdout_fraction": 0.2,
}


@dataclass
class PCAEncoder:
    config: EncoderConfig
    model_id: str = field(init=False)
    latent_names: list[str] = field(init=False)
    _scaler: StandardScaler | None = field(default=None, repr=False)
    _pca: PCA | None = field(default=None, repr=False)
    _feature_columns: list[str] = field(default_factory=list, repr=False)
    _val_metrics: dict[str, float] = field(default_factory=dict, repr=False)

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
        missing = [
            name for name in self.config.input_features if name not in window.columns
        ]
        if missing:
            raise ValueError(f"Input features missing from window: {missing}")
        return list(self.config.input_features)

    def _split_fit_validation(
        self, values: np.ndarray, holdout_fraction: float
    ) -> tuple[np.ndarray, np.ndarray]:
        if values.shape[0] < 2:
            return values, values[:0]
        holdout_rows = max(1, int(round(values.shape[0] * holdout_fraction)))
        if holdout_rows >= values.shape[0]:
            holdout_rows = max(1, values.shape[0] // 5)
        split_at = values.shape[0] - holdout_rows
        if split_at < 1:
            split_at = 1
        return values[:split_at], values[split_at:]

    def fit(self, window: pd.DataFrame) -> None:
        if window.empty:
            raise ValueError("Training window must not be empty")

        hyperparams = self._resolved_hyperparams()
        self._feature_columns = self._select_feature_columns(window)
        # Classical feature windows carry leading warm-up NaNs; sklearn PCA rejects
        # NaN, so fit on complete rows only (chronological order preserved).
        complete = window[self._feature_columns].astype(float).dropna(axis=0, how="any")
        if complete.empty:
            raise ValueError("Training window has no rows without missing feature values")
        raw = complete.to_numpy()

        fit_values, val_values = self._split_fit_validation(
            raw,
            float(hyperparams["val_holdout_fraction"]),
        )

        scaler = StandardScaler()
        scaled_fit = scaler.fit_transform(fit_values)

        n_components = min(self.config.n_latents, scaled_fit.shape[1], scaled_fit.shape[0])
        if n_components < 1:
            raise ValueError("Not enough rows or features to fit PCA")

        pca = PCA(
            n_components=n_components,
            svd_solver=str(hyperparams["svd_solver"]),
            random_state=int(hyperparams["random_state"]),
        )
        pca.fit(scaled_fit)

        self._scaler = scaler
        self._pca = pca
        self._val_metrics = self._compute_validation_metrics(val_values)

    def _compute_validation_metrics(self, val_values: np.ndarray) -> dict[str, float]:
        if val_values.size == 0 or self._scaler is None or self._pca is None:
            explained = float(self._pca.explained_variance_ratio_.sum()) if self._pca else 0.0
            return {
                "reconstruction_r2": explained,
                "explained_variance": explained,
            }

        scaled_val = self._scaler.transform(val_values)
        encoded = self._pca.transform(scaled_val)
        reconstructed = self._pca.inverse_transform(encoded)
        reconstruction_r2 = float(r2_score(scaled_val, reconstructed))
        explained_variance = float(self._pca.explained_variance_ratio_.sum())
        return {
            "reconstruction_r2": reconstruction_r2,
            "explained_variance": explained_variance,
        }

    def transform(self, window: pd.DataFrame) -> pd.DataFrame:
        if self._scaler is None or self._pca is None:
            raise RuntimeError("PCAEncoder must be fit before transform")

        if window.empty:
            return pd.DataFrame(columns=self.latent_names, index=window.index)

        feature_columns = self._select_feature_columns(window)
        features = window[feature_columns].astype(float)
        # Rows with any missing feature (warm-up or gaps) can't be encoded — emit NaN
        # latents for them and transform only the complete rows, preserving the index.
        complete_mask = features.notna().all(axis=1).to_numpy()
        latent_values = np.full((len(features), len(self.latent_names)), np.nan, dtype=float)
        if complete_mask.any():
            scaled = self._scaler.transform(features.to_numpy()[complete_mask])
            encoded = self._pca.transform(scaled)
            latent_values[complete_mask, : encoded.shape[1]] = encoded

        latent_frame = pd.DataFrame(
            latent_values,
            index=window.index,
            columns=self.latent_names,
        )
        return latent_frame[self.latent_names]

    def dump_artifact_state(self) -> dict[str, Any]:
        return {
            "scaler": self._scaler,
            "pca": self._pca,
            "feature_columns": list(self._feature_columns),
            "val_metrics": dict(self._val_metrics),
        }

    @classmethod
    def load_from_artifact_state(
        cls, config: EncoderConfig, state: dict[str, Any]
    ) -> PCAEncoder:
        encoder = cls(config=config)
        encoder._scaler = state.get("scaler")
        encoder._pca = state.get("pca")
        encoder._feature_columns = list(state.get("feature_columns", []))
        encoder._val_metrics = dict(state.get("val_metrics", {}))
        return encoder
