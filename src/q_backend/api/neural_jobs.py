"""Job manager for asynchronous neural encoder training (WO147)."""

from __future__ import annotations

import logging
import uuid
from typing import Any, Literal, Optional

from q_backend.api.schemas.neural import NeuralTrainRequest
from q_backend.neural.training import default_train_encoder_config
from q_backend.neural.training_pipeline import (
    TrainEncoderEvaluateSpec,
    run_train_encoder_pipeline,
)
from q_backend.storage.db.engine import session_scope
from q_backend.storage.redis.client import get_redis
from q_backend.storage.redis.progress import get_job_progress, set_job_progress

logger = logging.getLogger(__name__)

PROGRESS_NAMESPACE = "neural_training"

JobStatus = Literal["queued", "running", "completed", "failed"]


def _gate_payload(gate_result) -> dict[str, Any]:
    return {
        "baseline_ic": gate_result.baseline_ic,
        "best_latent_ic": gate_result.best_latent_ic,
        "n_latents_beating_baseline": gate_result.n_latents_beating_baseline,
        "passed": gate_result.passed,
        "evaluation_run_id": gate_result.evaluation_run_id,
    }


def _persist_progress(job_id: str, payload: dict[str, Any]) -> None:
    try:
        set_job_progress(
            get_redis(),
            job_id,
            payload,
            namespace=PROGRESS_NAMESPACE,
        )
    except Exception:  # noqa: BLE001 - best-effort Redis progress; logged
        logger.debug("Redis progress unavailable for neural training job %s", job_id)


def _base_payload(job_id: str, *, status: JobStatus, progress: str | None = None) -> dict[str, Any]:
    return {
        "job_id": job_id,
        "status": status,
        "progress": progress,
        "model_hash": None,
        "val_metrics": None,
        "gate": None,
        "gate_error": None,
        "error": None,
    }


def start_training_job(*, request: NeuralTrainRequest) -> dict[str, str]:
    """Validate, persist the job record, and enqueue the training actor."""
    job_id = str(uuid.uuid4())
    _persist_progress(
        job_id,
        _base_payload(job_id, status="queued", progress="queued"),
    )

    from q_backend.tasks import actors

    actors.run_neural_training.send(job_id, request.model_dump_json())
    return {"job_id": job_id, "status": "queued"}


def run_training_job(job_id: str, request_json: str) -> None:
    """Worker entry point: train the encoder and optionally run the IC gate."""
    request = NeuralTrainRequest.model_validate_json(request_json)

    def on_progress(status: str, progress: str | None) -> None:
        _persist_progress(
            job_id,
            {
                **_base_payload(job_id, status=status, progress=progress),
            },
        )

    try:
        on_progress("running", "building_window")
        config = default_train_encoder_config(
            kind=request.kind,
            symbol=request.symbol,
            timeframe=request.timeframe,
            train_start=request.train_start,
            train_end=request.train_end,
            n_latents=request.n_latents,
            input_features=tuple(request.input_features),
            model_key=request.model_key,
            hyperparams=request.hyperparams,
        )
        evaluate = (
            TrainEncoderEvaluateSpec(
                target=request.evaluate.target,
                horizon=request.evaluate.horizon,
            )
            if request.evaluate is not None
            else None
        )

        with session_scope() as session:
            result = run_train_encoder_pipeline(
                session,
                config,
                input_features=tuple(request.input_features),
                evaluate=evaluate,
                on_progress=on_progress,
            )

        _persist_progress(
            job_id,
            {
                **_base_payload(job_id, status="completed", progress="done"),
                "model_hash": result.model_hash,
                "val_metrics": result.val_metrics,
                "gate": _gate_payload(result.gate) if result.gate is not None else None,
                "gate_error": result.gate_error,
            },
        )
    except Exception as exc:  # noqa: BLE001 — surface any failure to the client
        logger.exception("Neural training job %s failed", job_id)
        _persist_progress(
            job_id,
            {
                **_base_payload(job_id, status="failed", progress="failed"),
                "error": str(exc),
            },
        )


def get_training_status_payload(job_id: str) -> Optional[dict[str, Any]]:
    try:
        return get_job_progress(get_redis(), job_id, namespace=PROGRESS_NAMESPACE)
    except Exception:  # noqa: BLE001 - best-effort Redis progress; logged
        logger.debug("Redis progress unavailable for neural training job %s", job_id)
        return None
