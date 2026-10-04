from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Literal

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from q_backend.ml_filters.adapters import FittedEntryModel
from q_backend.ml_filters.artifacts import (
    load_model_version,
    read_dataset_manifest,
    read_ml_filter_result,
    read_model_manifest,
    write_ml_filter_result,
)
from q_backend.ml_filters.compatibility import (  # noqa: F401 - re-exported compatibility API
    MLFilterCompatibilityError,
    MLFilterModelUnavailableError,
    MLFilterRequestError,
    validate_filter_compatibility,
    validate_filter_request_shape,
)
from q_backend.ml_filters.evaluation import compare_filters, evaluate_filter
from q_backend.ml_filters.training import train_filters
from q_backend.storage.db.engine import session_scope
from q_backend.storage.db.models import MLFilterModelVersion, MLFilterRun
from q_backend.storage.db.repositories import (
    create_ml_filter_evaluation,
    create_ml_filter_model_version,
    create_ml_filter_run,
    get_ml_filter_evaluation_by_dataset,
    get_ml_filter_model_version,
    get_ml_filter_run,
    update_ml_filter_run,
)

logger = logging.getLogger(__name__)

RunType = Literal["training", "comparison", "evaluation"]


def _job_uuid(job_id: str) -> uuid.UUID:
    try:
        return uuid.UUID(job_id)
    except ValueError as exc:
        raise ValueError("job_id is malformed") from exc


def _payload(request: Any) -> dict[str, Any]:
    return request.model_dump(mode="json") if hasattr(request, "model_dump") else dict(request)


def _dispatch(job_id: str, request: dict[str, Any], run_type: RunType) -> None:
    from q_backend.tasks import actors

    try:
        actors.run_ml_filter_job.send(job_id, json.dumps(request), run_type)
    except Exception as exc:
        with session_scope() as session:
            update_ml_filter_run(
                session,
                _job_uuid(job_id),
                status="failed",
                error_message=f"Unable to enqueue ML filter job: {exc}",
                finished_at=datetime.now(timezone.utc),
            )
        raise


def start_training_job(request: Any) -> dict[str, str]:
    payload = _payload(request)
    with session_scope() as session:
        row = create_ml_filter_run(
            session,
            run_type="training",
            request=payload,
            source_run_id=payload["source_run_id"],
            stage="dataset",
        )
        job_id = row.id.hex
    _dispatch(job_id, payload, "training")
    return {"job_id": job_id, "status": "queued"}


def start_comparison_job(request: Any) -> dict[str, str]:
    payload = _payload(request)
    with session_scope() as session:
        for model_id in payload["model_version_ids"]:
            model = get_ml_filter_model_version(session, model_id)
            if model is None or model.status != "ready":
                raise FileNotFoundError(f"Ready ML filter model '{model_id}' was not found")
            if model.dataset_id != payload["dataset_id"]:
                raise ValueError("All selected model versions must share the requested dataset")
        row = create_ml_filter_run(
            session,
            run_type="comparison",
            request=payload,
            dataset_id=payload["dataset_id"],
            stage="validation",
        )
        job_id = row.id.hex
    _dispatch(job_id, payload, "comparison")
    return {"job_id": job_id, "status": "queued"}


def start_evaluation_job(request: Any) -> dict[str, str]:
    payload = _payload(request)
    selection = {
        "dataset_id": payload["dataset_id"],
        "model_version_id": payload["model_version_id"],
        "threshold": float(payload.get("threshold", 0.5)),
    }
    should_dispatch = False
    try:
        with session_scope() as session:
            model = get_ml_filter_model_version(session, selection["model_version_id"])
            if model is None or model.status != "ready":
                raise FileNotFoundError(f"Ready ML filter model '{selection['model_version_id']}' was not found")
            if model.dataset_id != selection["dataset_id"]:
                raise ValueError("Selected model version belongs to a different dataset")
            existing = get_ml_filter_evaluation_by_dataset(session, selection["dataset_id"])
            job_status = "queued"
            if existing is not None:
                if existing.selection != selection:
                    raise LockboxConsumedError(
                        "This dataset lockbox is already reserved for a different model and threshold"
                    )
                run = get_ml_filter_run(session, existing.run_id)
                if run is None:
                    raise RuntimeError("Reserved lockbox evaluation has no job record")
                job_id = run.id.hex
                job_status = run.status
                if run.status == "failed":
                    update_ml_filter_run(
                        session,
                        run.id,
                        status="queued",
                        stage="evaluation",
                        error_message=None,
                        finished_at=None,
                    )
                    should_dispatch = True
                    job_status = "queued"
            else:
                run = create_ml_filter_run(
                    session,
                    run_type="evaluation",
                    request=selection,
                    dataset_id=selection["dataset_id"],
                    stage="evaluation",
                )
                create_ml_filter_evaluation(
                    session,
                    dataset_id=selection["dataset_id"],
                    model_version_id=selection["model_version_id"],
                    threshold=selection["threshold"],
                    selection=selection,
                    run_id=run.id,
                )
                job_id = run.id.hex
                should_dispatch = True
                job_status = "queued"
    except IntegrityError:
        # The dataset unique constraint serializes concurrent lockbox claims.
        with session_scope() as session:
            existing = get_ml_filter_evaluation_by_dataset(session, selection["dataset_id"])
            if existing is None or existing.selection != selection:
                raise LockboxConsumedError(
                    "This dataset lockbox is already reserved for a different model and threshold"
                )
            job_id = existing.run_id.hex
            run = get_ml_filter_run(session, existing.run_id)
            if run is None:
                raise RuntimeError("Reserved lockbox evaluation has no job record")
            job_status = run.status
    if should_dispatch:
        _dispatch(job_id, selection, "evaluation")
    return {"job_id": job_id, "status": job_status}


class LockboxConsumedError(ValueError):
    pass


def _claim_job(job_id: str) -> bool:
    run_id = _job_uuid(job_id)
    with session_scope() as session:
        result = session.execute(
            update(MLFilterRun)
            .where(MLFilterRun.id == run_id, MLFilterRun.status.in_(["queued", "failed"]))
            .values(status="running", started_at=datetime.now(timezone.utc), error_message=None)
        )
        if result.rowcount:
            return True
        run = get_ml_filter_run(session, run_id)
        return False if run is None or run.status in {"running", "completed"} else False


def _progress(job_id: str, **values: Any) -> None:
    with session_scope() as session:
        run = get_ml_filter_run(session, _job_uuid(job_id))
        if run is None:
            return
        updates: dict[str, Any] = {"status": "running"}
        if "stage" in values:
            updates["stage"] = values["stage"]
        progress_values = {name: value for name, value in values.items() if name != "stage"}
        if progress_values:
            updates["progress"] = {**(run.progress or {}), **progress_values}
        if "dataset_id" in values:
            updates["dataset_id"] = values["dataset_id"]
        update_ml_filter_run(session, run.id, **updates)


def run_ml_filter_job(job_id: str, request_json: str, run_type: RunType) -> None:
    if not _claim_job(job_id):
        return
    payload = json.loads(request_json)
    try:
        if run_type == "training":
            result = train_filters(payload, job_id, progress_callback=lambda **kw: _progress(job_id, **kw))
        elif run_type == "comparison":
            _progress(job_id, stage="validation")
            result = compare_filters(payload, job_id=job_id)
        elif run_type == "evaluation":
            _progress(job_id, stage="evaluation")
            result = evaluate_filter(payload, job_id=job_id)
        else:
            raise ValueError(f"Unsupported ML filter run type: {run_type}")
        if run_type == "training":
            _progress(job_id, stage="persisting")
        result_path = write_ml_filter_result(job_id, result)
        with session_scope() as session:
            run = get_ml_filter_run(session, _job_uuid(job_id))
            if run is None:
                raise RuntimeError(f"ML filter job '{job_id}' disappeared before completion")
            if run_type == "training":
                source_id = result["source_run_id"]
                for model in result["model_versions"]:
                    path = model["artifact_path"]
                    create_ml_filter_model_version(
                        session,
                        model_version_id=model["model_version_id"],
                        dataset_id=result["dataset_id"],
                        source_run_id=source_id,
                        algorithm=model["algorithm"],
                        artifact_path=path,
                        manifest_path=f"{path}/manifest.json",
                        summary={
                            "symbol": result.get("symbol", ""),
                            "timeframe": result.get("timeframe", ""),
                            "train_end": result.get("train_end"),
                            "selected_features": result.get("selected_features", []),
                            "validation_metrics": model.get("validation_metrics", {}),
                            "source_run_id": source_id,
                        },
                    )
            update_ml_filter_run(
                session,
                run.id,
                status="completed",
                stage="persisting",
                dataset_id=result.get("dataset_id", run.dataset_id),
                result_summary={
                    key: value
                    for key, value in result.items()
                    if key not in {"validation_comparison", "results", "baseline", "filtered"}
                },
                lake_paths={"result": result_path},
                finished_at=datetime.now(timezone.utc),
                progress={"current": 1, "total": 1},
            )
    except Exception as exc:  # noqa: BLE001 - worker failures must be durable and visible
        logger.exception("ML filter %s job %s failed", run_type, job_id)
        with session_scope() as session:
            run = get_ml_filter_run(session, _job_uuid(job_id))
            if run is not None:
                update_ml_filter_run(
                    session,
                    run.id,
                    status="failed",
                    error_message=str(exc),
                    finished_at=datetime.now(timezone.utc),
                )


def read_filter_manifest(model_version_id: str) -> dict[str, Any]:
    """Return the ready, checksum-verified model manifest merged with its dataset fields.

    Raises ``MLFilterModelUnavailableError`` for a missing, deleted, not-ready or
    corrupt model; a different version is never substituted.
    """
    with session_scope() as session:
        row = get_ml_filter_model_version(session, model_version_id)
        ready = row is not None and row.status == "ready"
    if not ready:
        raise MLFilterModelUnavailableError(f"Ready ML filter model '{model_version_id}' was not found")
    try:
        manifest = read_model_manifest(model_version_id)
        dataset = read_dataset_manifest(manifest["dataset_id"])
    except (FileNotFoundError, ValueError, KeyError) as exc:
        raise MLFilterModelUnavailableError(
            f"ML filter model '{model_version_id}' is unavailable or corrupt: {exc}"
        ) from exc
    return {
        **manifest,
        "compatibility_fingerprint": dataset["compatibility_fingerprint"],
        "train_end": dataset["train_end"],
    }


def resolve_filter_model(config: dict[str, Any]) -> tuple[FittedEntryModel, dict[str, Any]]:
    """Validate a backtest config against its pinned model and load the pipeline once."""
    validate_filter_request_shape(config)
    model_version_id = config["ml_filter"]["model_version_id"]
    manifest = read_filter_manifest(model_version_id)
    validate_filter_compatibility(config, manifest)
    try:
        fitted = load_model_version(model_version_id)
    except (FileNotFoundError, ValueError) as exc:
        raise MLFilterModelUnavailableError(
            f"ML filter model '{model_version_id}' is unavailable or corrupt: {exc}"
        ) from exc
    return fitted, manifest


def get_job_status(job_id: str, expected_type: RunType | None = None) -> dict[str, Any] | None:
    with session_scope() as session:
        run = get_ml_filter_run(session, _job_uuid(job_id))
        if run is None or (expected_type and run.run_type != expected_type):
            return None
        result_ref = (run.lake_paths or {}).get("result")
        summary = dict(run.result_summary or {})
        request = dict(run.request or {})
        row = {
            "job_id": run.id.hex,
            "status": run.status,
            "stage": run.stage,
            "dataset_id": run.dataset_id,
            "request": request,
            "progress": run.progress,
            "result_summary": summary,
            "error_message": run.error_message,
            "result_ref": result_ref,
            "run_type": run.run_type,
        }
    if result_ref:
        try:
            row["result"] = read_ml_filter_result(result_ref)
        except FileNotFoundError:
            row["result"] = None
    if run.run_type == "evaluation":
        row.update(
            model_version_id=request.get("model_version_id"),
            threshold=request.get("threshold"),
        )
    return row
