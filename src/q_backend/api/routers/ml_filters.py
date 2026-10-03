from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from q_backend.api import ml_filter_jobs
from q_backend.api.deps import get_session
from q_backend.api.schemas.ml_filters import (
    ComparisonRequest,
    ComparisonStatusResponse,
    EvaluationRequest,
    EvaluationStatusResponse,
    JobStartResponse,
    ModelDetailResponse,
    ModelListResponse,
    SourceDetailResponse,
    SourceListResponse,
    TrainingRequest,
    TrainingStatusResponse,
)
from q_backend.ml_filters.artifacts import load_model_version, read_model_manifest
from q_backend.ml_filters.service import LockboxConsumedError
from q_backend.storage.db.repositories import (
    get_ml_filter_model_version,
    list_ml_filter_model_versions,
)

router = APIRouter(tags=["ml-filters"])


@router.get("/api/v1/ml-filters/sources", response_model=SourceListResponse)
def list_ml_filter_sources(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(get_session),
):
    items, total = ml_filter_jobs.list_source_summaries(session, limit=limit, offset=offset)
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@router.get("/api/v1/ml-filters/sources/{run_id}", response_model=SourceDetailResponse)
def get_ml_filter_source(run_id: str, session: Session = Depends(get_session)):
    item = ml_filter_jobs.get_source_summary(session, run_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Backtest source was not found")
    return item


@router.post("/api/v1/ml-filters/training", response_model=JobStartResponse, status_code=status.HTTP_202_ACCEPTED)
def start_ml_filter_training(body: TrainingRequest):
    source = ml_filter_jobs.get_source_summary_by_id(body.source_run_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Backtest source was not found")
    if not source["eligible"]:
        raise HTTPException(
            status_code=422,
            detail={"code": "incompatible_source", "message": source["eligibility_reason"], "details": source},
        )
    try:
        return ml_filter_jobs.start_training(body)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail={"code": "invalid_split", "message": str(exc)}) from exc


@router.get("/api/v1/ml-filters/training/{job_id}", response_model=TrainingStatusResponse)
def get_ml_filter_training(job_id: str):
    payload = ml_filter_jobs.get_training_status(job_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="ML filter training job was not found")
    return payload


@router.get("/api/v1/ml-filters/models", response_model=ModelListResponse)
def list_ml_filter_models(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    dataset_id: str | None = None,
    symbol: str | None = None,
    timeframe: str | None = None,
    session: Session = Depends(get_session),
):
    items, total = list_ml_filter_model_versions(
        session,
        dataset_id=dataset_id,
        symbol=symbol,
        timeframe=timeframe,
        limit=limit,
        offset=offset,
    )
    summaries = []
    for item in items:
        reasons: list[str] = []
        try:
            manifest = read_model_manifest(item.model_version_id)
            load_model_version(item.model_version_id)
        except (FileNotFoundError, ValueError) as exc:
            manifest = {}
            reasons.append(str(exc))
        summary = item.summary or {}
        summaries.append(
            {
                "model_version_id": item.model_version_id,
                "dataset_id": item.dataset_id,
                "algorithm": item.algorithm,
                "ready": item.status == "ready" and not reasons,
                "selected_features": [feature["name"] for feature in manifest.get("selected_features", [])]
                or summary.get("selected_features", []),
                "symbol": summary.get("symbol", ""),
                "timeframe": summary.get("timeframe", ""),
                "train_end": summary.get("train_end"),
                "compatibility_reasons": reasons,
            }
        )
    return {"items": summaries, "total": total, "limit": limit, "offset": offset}


@router.get("/api/v1/ml-filters/models/{model_version_id}", response_model=ModelDetailResponse)
def get_ml_filter_model(model_version_id: str, session: Session = Depends(get_session)):
    row = get_ml_filter_model_version(session, model_version_id)
    if row is None:
        raise HTTPException(status_code=404, detail="ML filter model version was not found")
    try:
        manifest = read_model_manifest(model_version_id)
        load_model_version(model_version_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail={"code": "artifact_unavailable", "message": str(exc)}) from exc
    summary = row.summary or {}
    return {
        "model_version_id": row.model_version_id,
        "dataset_id": row.dataset_id,
        "algorithm": row.algorithm,
        "selected_features": [feature["name"] for feature in manifest.get("selected_features", [])],
        "manifest_identity": {
            "format_version": manifest.get("format_version"),
            "model_content_id": manifest.get("model_content_id"),
            "fitted_artifact_checksum": manifest.get("fitted_artifact_checksum"),
        },
        "pipeline_versions": manifest.get("dependency_versions", {}),
        "provenance": {
            "source_run_id": row.source_run_id,
            "hyperparameters": manifest.get("hyperparameters", {}),
            "seed": manifest.get("seed"),
            "training_label_availability_cutoff": manifest.get("training_label_availability_cutoff"),
            "compatibility_fingerprint": summary.get("compatibility_fingerprint"),
        },
        "validation_metrics": summary.get("validation_metrics", {}),
    }


@router.post("/api/v1/ml-filters/comparisons", response_model=JobStartResponse, status_code=status.HTTP_202_ACCEPTED)
def start_ml_filter_comparison(body: ComparisonRequest):
    try:
        ml_filter_jobs.ensure_dataset_available(body.dataset_id)
        return ml_filter_jobs.start_comparison(body)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail={"code": "incompatible_model", "message": str(exc)}) from exc


@router.get("/api/v1/ml-filters/comparisons/{job_id}", response_model=ComparisonStatusResponse)
def get_ml_filter_comparison(job_id: str):
    payload = ml_filter_jobs.get_comparison_status(job_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="ML filter comparison job was not found")
    return payload


@router.post("/api/v1/ml-filters/evaluations", response_model=JobStartResponse, status_code=status.HTTP_202_ACCEPTED)
def start_ml_filter_evaluation(body: EvaluationRequest):
    try:
        ml_filter_jobs.ensure_dataset_available(body.dataset_id)
        return ml_filter_jobs.start_evaluation(body)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except LockboxConsumedError as exc:
        raise HTTPException(status_code=409, detail={"code": "lockbox_consumed", "message": str(exc)}) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail={"code": "incompatible_model", "message": str(exc)}) from exc


@router.get("/api/v1/ml-filters/evaluations/{job_id}", response_model=EvaluationStatusResponse)
def get_ml_filter_evaluation(job_id: str):
    payload = ml_filter_jobs.get_evaluation_status(job_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="ML filter evaluation job was not found")
    return payload
