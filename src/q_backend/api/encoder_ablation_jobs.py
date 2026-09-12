"""Job manager for encoder ablation experiments (WO155)."""

from __future__ import annotations

import logging
import math
import uuid
from datetime import datetime, timezone
from typing import Any, Literal, Optional

from q_backend.api.schemas.experiments import (
    EncoderAblationRequest,
    EncoderAblationResult,
    EncoderAblationRow,
    EncoderConfigSpec,
)
from q_backend.neural.training import default_train_encoder_config
from q_backend.neural.training_pipeline import (
    TrainEncoderEvaluateSpec,
    run_train_encoder_pipeline,
)
from q_backend.storage.db.engine import session_scope
from q_backend.storage.lake.artifacts import write_encoder_ablation_result
from q_backend.storage.redis.client import get_redis
from q_backend.storage.redis.progress import get_job_progress, set_job_progress

logger = logging.getLogger(__name__)

PROGRESS_NAMESPACE = "encoder_ablation"

JobStatus = Literal["queued", "running", "completed", "failed"]

_FAST_AE_HYPERPARAMS: dict[str, Any] = {
    "lookback": 8,
    "hidden_dim": 16,
    "num_layers": 1,
    "epochs": 4,
    "batch_size": 16,
    "random_state": 0,
}


def _encoder_kind_to_train_kind(encoder_kind: str) -> str:
    return "autoencoder" if encoder_kind == "ae" else encoder_kind


def _ablation_model_key(symbol: str, label: str) -> str:
    safe_label = label.lower().replace("$", "").replace("/", "_").replace(":", "_")
    symbol_key = symbol.lower().replace("$", "").replace("/", "_")
    return f"ablation_{symbol_key}_{safe_label}"


def _coerce_datetime(value: datetime | str) -> datetime:
    if isinstance(value, datetime):
        return value
    ts = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def _config_window(
    request: EncoderAblationRequest,
    hyperparams: dict[str, Any],
) -> tuple[datetime, datetime, dict[str, Any]]:
    hp = dict(hyperparams)
    train_start = _coerce_datetime(hp.pop("train_start", request.train_start))
    train_end = _coerce_datetime(hp.pop("train_end", request.train_end))
    return train_start, train_end, hp


def _merge_hyperparams(spec: EncoderConfigSpec) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    if spec.encoder_kind == "ae":
        merged.update(_FAST_AE_HYPERPARAMS)
    merged.update(spec.hyperparams)
    return merged


def _finite_or_none(value: float | None) -> float | None:
    if value is None:
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return float(value)


def _row_from_pipeline_result(
    spec: EncoderConfigSpec,
    *,
    model_hash: str | None,
    val_metrics: dict[str, Any] | None,
    gate,
    gate_error: str | None,
) -> EncoderAblationRow:
    recon_r2 = None
    if val_metrics is not None:
        recon_r2 = _finite_or_none(val_metrics.get("reconstruction_r2"))

    best_latent_ic = None
    baseline_ic = None
    passed: bool | None = None
    if gate is not None:
        best_latent_ic = _finite_or_none(gate.best_latent_ic)
        baseline_ic = _finite_or_none(gate.baseline_ic)
        passed = gate.passed

    ic_delta = None
    if best_latent_ic is not None and baseline_ic is not None:
        ic_delta = best_latent_ic - baseline_ic

    return EncoderAblationRow(
        label=spec.label,
        encoder_kind=spec.encoder_kind,
        model_hash=model_hash,
        recon_r2=recon_r2,
        best_latent_ic=best_latent_ic,
        baseline_ic=baseline_ic,
        ic_delta_vs_baseline=ic_delta,
        passed=passed,
        gate_error=gate_error,
    )


def _select_best_label(rows: list[EncoderAblationRow]) -> str | None:
    candidates = [row for row in rows if row.best_latent_ic is not None and math.isfinite(row.best_latent_ic)]
    if not candidates:
        return None
    best = max(
        candidates,
        key=lambda row: (
            row.best_latent_ic,
            row.recon_r2 if row.recon_r2 is not None else float("-inf"),
        ),
    )
    return best.label


def _persist_progress(job_id: str, payload: dict[str, Any]) -> None:
    try:
        set_job_progress(
            get_redis(),
            job_id,
            payload,
            namespace=PROGRESS_NAMESPACE,
        )
    except Exception:  # noqa: BLE001 - best-effort Redis progress; logged
        logger.debug("Redis progress unavailable for encoder ablation job %s", job_id)


def _base_payload(
    job_id: str,
    *,
    status: JobStatus,
    progress: str | None = None,
) -> dict[str, Any]:
    return {
        "job_id": job_id,
        "status": status,
        "progress": progress,
        "result": None,
        "error": None,
    }


def start_encoder_ablation_job(*, request: EncoderAblationRequest) -> dict[str, str]:
    """Validate, persist the job record, and enqueue the ablation actor."""
    job_id = str(uuid.uuid4())
    _persist_progress(
        job_id,
        _base_payload(job_id, status="queued", progress="queued"),
    )

    from q_backend.tasks import actors

    actors.run_encoder_ablation.send(job_id, request.model_dump_json())
    return {"job_id": job_id, "status": "queued"}


def run_encoder_ablation_job(job_id: str, request_json: str) -> None:
    """Worker entry point: train each encoder config and compare gate outcomes."""
    request = EncoderAblationRequest.model_validate_json(request_json)
    total = len(request.configs)
    rows: list[EncoderAblationRow] = []
    input_features = tuple(request.input_features)

    def on_progress(done: int) -> None:
        _persist_progress(
            job_id,
            _base_payload(
                job_id,
                status="running",
                progress=f"{done}/{total}",
            ),
        )

    try:
        on_progress(0)
        evaluate = TrainEncoderEvaluateSpec(
            target=request.target,
            horizon=request.horizon,
        )

        for index, spec in enumerate(request.configs, start=1):
            try:
                with session_scope() as session:
                    merged_hyperparams = _merge_hyperparams(spec)
                    train_start, train_end, encoder_hyperparams = _config_window(
                        request,
                        merged_hyperparams,
                    )
                    config = default_train_encoder_config(
                        kind=_encoder_kind_to_train_kind(spec.encoder_kind),
                        symbol=request.symbol,
                        timeframe=request.timeframe,
                        train_start=train_start,
                        train_end=train_end,
                        n_latents=request.n_latents,
                        input_features=input_features,
                        model_key=_ablation_model_key(request.symbol, spec.label),
                        hyperparams=encoder_hyperparams,
                    )
                    result = run_train_encoder_pipeline(
                        session,
                        config,
                        input_features=input_features,
                        evaluate=evaluate,
                    )
                rows.append(
                    _row_from_pipeline_result(
                        spec,
                        model_hash=result.model_hash,
                        val_metrics=result.val_metrics,
                        gate=result.gate,
                        gate_error=result.gate_error,
                    )
                )
            except Exception as exc:  # noqa: BLE001 — one bad config must not abort the batch
                logger.exception(
                    "Encoder ablation config %s failed in job %s",
                    spec.label,
                    job_id,
                )
                rows.append(
                    EncoderAblationRow(
                        label=spec.label,
                        encoder_kind=spec.encoder_kind,
                        gate_error=str(exc),
                    )
                )
            on_progress(index)

        ablation_result = EncoderAblationResult(
            rows=rows,
            best_label=_select_best_label(rows),
            symbol=request.symbol,
            timeframe=request.timeframe,
            target=request.target,
            horizon=request.horizon,
        )
        write_encoder_ablation_result(job_id, ablation_result.model_dump(mode="json"))
        _persist_progress(
            job_id,
            {
                **_base_payload(job_id, status="completed", progress=f"{total}/{total}"),
                "result": ablation_result.model_dump(mode="json"),
            },
        )
    except Exception as exc:  # noqa: BLE001 — surface orchestration failures to the client
        logger.exception("Encoder ablation job %s failed", job_id)
        _persist_progress(
            job_id,
            {
                **_base_payload(job_id, status="failed", progress="failed"),
                "error": str(exc),
            },
        )


def get_encoder_ablation_status_payload(job_id: str) -> Optional[dict[str, Any]]:
    try:
        return get_job_progress(get_redis(), job_id, namespace=PROGRESS_NAMESPACE)
    except Exception:  # noqa: BLE001 - best-effort Redis progress; logged
        logger.debug("Redis progress unavailable for encoder ablation job %s", job_id)
        return None


def reconcile_orphaned_runs() -> int:
    """Mark encoder-ablation jobs left running by a crashed worker as failed."""
    try:
        client = get_redis()
        count = 0
        for key in client.scan_iter(match=f"{PROGRESS_NAMESPACE}:progress:*"):
            job_id = key.rsplit(":", 1)[-1]
            payload = get_job_progress(client, job_id, namespace=PROGRESS_NAMESPACE)
            if payload is None or payload.get("status") != "running":
                continue
            _persist_progress(
                job_id,
                {
                    **payload,
                    "status": "failed",
                    "progress": "cancelled",
                    "error": "Cancelled after backend restart (run was orphaned).",
                },
            )
            count += 1
    except Exception as exc:  # noqa: BLE001 — startup reconcile must not crash boot
        logger.warning("Failed to reconcile orphaned encoder ablation runs: %s", exc)
        return 0
    if count:
        logger.info("Reconciled %d orphaned encoder ablation run(s) on startup.", count)
    return count
