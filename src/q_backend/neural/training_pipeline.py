"""Shared train + optional gate sequence for CLI and worker jobs (WO147)."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import pandas as pd
from sqlalchemy.orm import Session

from q_backend.features.matrix import FeatureRequest, build_feature_matrix
from q_backend.neural.gate import LatentGateResult, evaluate_latents
from q_backend.neural.training import TrainEncoderConfig, train_encoder

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[str, str | None], None]


@dataclass(frozen=True)
class TrainEncoderEvaluateSpec:
    target: str
    horizon: int


@dataclass(frozen=True)
class TrainEncoderPipelineResult:
    model_key: str
    model_hash: str
    version: int
    val_metrics: dict[str, Any]
    artifact_path: str
    gate: LatentGateResult | None = None
    gate_error: str | None = None


def build_training_feature_window(
    symbol: str,
    timeframe: str,
    start: datetime,
    end: datetime,
    input_features: tuple[str, ...],
) -> pd.DataFrame:
    requests = [
        FeatureRequest(name=name, version=None, params={}) for name in input_features
    ]
    matrix = build_feature_matrix(
        symbol,
        timeframe,
        start,
        end,
        requests,
        use_cache=True,
    )
    # ``build_feature_matrix`` keys columns by ``feature_id`` (e.g. ``rsi.v1.<hash>``),
    # but the encoder matches on the bare feature name. Rename via the manifest so the
    # window the encoder receives uses names — otherwise every feature reads as missing.
    names_by_id = {
        str(row["feature_id"]): str(row["name"])
        for row in matrix.manifest.get("features", [])
    }
    return matrix.frame.rename(columns=lambda fid: names_by_id.get(fid, fid))


def run_train_encoder_pipeline(
    session: Session,
    config: TrainEncoderConfig,
    *,
    input_features: tuple[str, ...],
    evaluate: TrainEncoderEvaluateSpec | None = None,
    on_progress: ProgressCallback | None = None,
) -> TrainEncoderPipelineResult:
    """Build the input window, train the encoder, and optionally run the IC gate."""

    def report(status: str, progress: str | None = None) -> None:
        if on_progress is not None:
            on_progress(status, progress)

    report("running", "building_window")

    def window_builder(symbol: str, timeframe: str, start: datetime, end: datetime):
        return build_training_feature_window(
            symbol,
            timeframe,
            start,
            end,
            input_features,
        )

    report("running", "training")
    version = train_encoder(
        session,
        config,
        window_builder=window_builder,
    )
    # Persist the trained model before the optional gate. Training and evaluation are
    # separate concerns: a gate failure (e.g. too few OOS bars after train_end) must
    # not roll back a model that trained successfully.
    session.commit()
    model_key = config.model_key
    model_hash = version.model_hash
    version_number = version.version
    val_metrics = dict(version.val_metrics)
    artifact_path = version.artifact_path

    gate_result: LatentGateResult | None = None
    gate_error: str | None = None
    if evaluate is not None:
        report("running", "evaluating")
        try:
            gate_result = evaluate_latents(
                session,
                version,
                target_name=evaluate.target,
                horizon=evaluate.horizon,
            )
            session.commit()
        except Exception as exc:  # noqa: BLE001 — gate is optional; keep the model
            session.rollback()
            gate_error = str(exc)
            logger.warning("Gate evaluation skipped for model %s: %s", model_hash, exc)

    report("running", "done")
    return TrainEncoderPipelineResult(
        model_key=model_key,
        model_hash=model_hash,
        version=version_number,
        val_metrics=val_metrics,
        artifact_path=artifact_path,
        gate=gate_result,
        gate_error=gate_error,
    )
