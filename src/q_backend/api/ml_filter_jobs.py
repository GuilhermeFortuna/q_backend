"""Durable job and source helpers for supervised ML entry filters."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.orm import Session

from q_backend.ml_filters.artifacts import load_dataset_snapshot
from q_backend.ml_filters.service import (
    LockboxConsumedError,
    get_job_status,
    start_comparison_job,
    start_evaluation_job,
    start_training_job,
)
from q_backend.ml_filters.source import get_source, list_sources
from q_backend.storage.db.engine import session_scope


def list_source_summaries(session: Session, *, limit: int, offset: int) -> tuple[list[dict[str, Any]], int]:
    return list_sources(session, limit=limit, offset=offset)


def get_source_summary(session: Session, run_id: str) -> dict[str, Any] | None:
    return get_source(session, run_id)


def get_source_summary_by_id(run_id: str) -> dict[str, Any] | None:
    try:
        uuid.UUID(run_id)
    except ValueError:
        return None
    with session_scope() as session:
        return get_source(session, run_id)


def ensure_dataset_available(dataset_id: str) -> None:
    load_dataset_snapshot(dataset_id)


def start_training(request: Any) -> dict[str, str]:
    return start_training_job(request)


def start_comparison(request: Any) -> dict[str, str]:
    return start_comparison_job(request)


def start_evaluation(request: Any) -> dict[str, str]:
    return start_evaluation_job(request)


def _status(job_id: str, expected_type: str) -> dict[str, Any] | None:
    try:
        return get_job_status(job_id, expected_type=expected_type)
    except ValueError:
        return None


def _error(row: dict[str, Any]) -> dict[str, Any] | None:
    message = row.get("error_message")
    if not message:
        return None
    lowered = message.lower()
    if "source" in lowered or "backtest" in lowered:
        code = "missing_source" if "not found" in lowered else "incompatible_source"
    elif "feature" in lowered or "volume" in lowered:
        code = "missing_features"
    elif "sample" in lowered or "class" in lowered:
        code = "insufficient_training_samples"
    elif "artifact" in lowered or "checksum" in lowered:
        code = "artifact_unavailable"
    elif "split" in lowered or "train_end" in lowered or "validation_end" in lowered:
        code = "invalid_split"
    elif "model" in lowered or "dataset" in lowered:
        code = "incompatible_model"
    elif "lockbox" in lowered:
        code = "lockbox_consumed"
    else:
        code = "training_failed"
    return {"code": code, "message": message}


def get_training_status(job_id: str) -> dict[str, Any] | None:
    row = _status(job_id, "training")
    if row is None:
        return None
    summary = row.get("result_summary", {})
    result = row.get("result") or {}
    saved_progress = row.get("progress") or {}
    progress = saved_progress.get("progress") if isinstance(saved_progress, dict) else None
    return {
        "job_id": row["job_id"],
        "status": row["status"],
        "stage": row.get("stage"),
        "progress": progress,
        "dataset_id": row.get("dataset_id") or summary.get("dataset_id"),
        "model_version_ids": [item["model_version_id"] for item in summary.get("model_versions", [])],
        "comparison_id": summary.get("comparison_id"),
        "rejections": saved_progress.get("rejections") or summary.get("rejections"),
        "error": _error(row),
    }


def _nullable_metric(value: Any, reason: str | None = None) -> dict[str, Any]:
    return {"value": value, "unavailable_reason": reason if value is None else None}


def validation_metrics_payload(classification: dict[str, Any]) -> dict[str, Any]:
    """Expose stored classification metrics using the versioned wire shape."""
    return {
        "confusion_matrix": classification.get("confusion_matrix"),
        "roc_auc": _nullable_metric(classification.get("roc_auc"), classification.get("roc_auc_reason")),
    }


def _engine_metrics(value: dict[str, Any]) -> dict[str, Any]:
    metrics = value.get("metrics", {})
    profit_factor = metrics.get("profit_factor")
    profit_factor_reason = None
    if not metrics.get("losing_trades"):
        profit_factor, profit_factor_reason = None, "Profit factor is undefined when there are no losing trades"
    return {
        "net_pnl": float(metrics.get("total_pnl", 0.0)),
        "max_drawdown": float(metrics.get("max_drawdown_value", 0.0)),
        "profit_factor": _nullable_metric(profit_factor, profit_factor_reason),
        "trade_count": int(value.get("trade_count", 0)),
    }


def get_comparison_status(job_id: str) -> dict[str, Any] | None:
    row = _status(job_id, "comparison")
    if row is None:
        return None
    result = row.get("result") or {}
    results = []
    for item in result.get("results", []):
        classification = item.get("classification", {})
        results.append(
            {
                "model_version_id": item["model_version_id"],
                "threshold": item["threshold"],
                "dataset_id": result.get("dataset_id"),
                "baseline": _engine_metrics(item.get("baseline", {})),
                "filtered": _engine_metrics(item.get("filtered", {})),
                "classification": validation_metrics_payload(classification),
                "acceptance_counts": {
                    "candidates_scored": int(item.get("filtered", {}).get("candidate_count", 0) or 0)
                    - int(item.get("filtered", {}).get("not_ready_count", 0) or 0),
                    "candidates_accepted": int(item.get("filtered", {}).get("accepted_count", 0) or 0),
                    "candidates_not_ready": int(item.get("filtered", {}).get("not_ready_count", 0) or 0),
                    "candidates_rejected": max(
                        0,
                        int(item.get("filtered", {}).get("candidate_count", 0) or 0)
                        - int(item.get("filtered", {}).get("not_ready_count", 0) or 0)
                        - int(item.get("filtered", {}).get("accepted_count", 0) or 0),
                    ),
                },
                "equity_artifact_ref": (item.get("filtered_artifacts") or {}).get("equity"),
                "trades_artifact_ref": (item.get("filtered_artifacts") or {}).get("trades"),
            }
        )
    return {
        "job_id": row["job_id"],
        "status": row["status"],
        "results": results or None,
        "error": _error(row),
    }


def get_evaluation_status(job_id: str) -> dict[str, Any] | None:
    row = _status(job_id, "evaluation")
    if row is None:
        return None
    result = row.get("result") or {}
    final = None
    if result:
        final = {
            "baseline": _engine_metrics(result.get("baseline", {})),
            "filtered": _engine_metrics(result.get("filtered", {})),
        }
    return {
        "job_id": row["job_id"],
        "status": row["status"],
        "dataset_id": row.get("dataset_id"),
        "model_version_id": row.get("model_version_id"),
        "threshold": row.get("threshold"),
        "result": final,
        "error": _error(row),
    }
