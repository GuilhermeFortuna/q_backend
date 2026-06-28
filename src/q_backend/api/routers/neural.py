from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from q_backend.api import neural_jobs
from q_backend.api.deps import get_session
from q_backend.api.schemas.neural import (
    LatentGateResultResponse,
    NeuralModelDetailResponse,
    NeuralModelListItem,
    NeuralModelListResponse,
    NeuralModelStatusUpdateRequest,
    NeuralTrainRequest,
    NeuralTrainStartResponse,
    NeuralTrainStatusResponse,
)
from q_backend.neural.gate import find_latest_latent_evaluation_run, read_latest_latent_gate_result
from q_backend.neural.promotion import (
    IllegalNeuralStatusTransition,
    NeuralModelNotFoundError,
    promote_neural_model,
)
from q_backend.storage.db.models import NeuralModelVersion
from q_backend.storage.db.repositories import (
    get_neural_model_version,
    list_neural_model_versions,
)

router = APIRouter(tags=["neural"])


def _list_item(version: NeuralModelVersion) -> NeuralModelListItem:
    if version.model is None:
        raise ValueError(f"Neural model version '{version.model_hash}' has no parent model.")
    return NeuralModelListItem(
        model_hash=version.model_hash,
        model_key=version.model.model_key,
        symbol=version.model.symbol,
        timeframe=version.model.timeframe,
        version=version.version,
        status=version.status,
        n_latents=version.n_latents,
        created_at=version.created_at,
        val_metrics=dict(version.val_metrics),
    )


def _gate_result_response(
    session: Session, version: NeuralModelVersion
) -> Optional[LatentGateResultResponse]:
    gate = read_latest_latent_gate_result(session, version)
    if gate is None or gate.evaluation_run_id is None:
        return None

    run = find_latest_latent_evaluation_run(session, version)
    return LatentGateResultResponse(
        evaluation_run_id=gate.evaluation_run_id,
        baseline_ic=gate.baseline_ic,
        best_latent_ic=gate.best_latent_ic,
        n_latents_beating_baseline=gate.n_latents_beating_baseline,
        passed=gate.passed,
        target_name=run.target_name if run is not None else None,
        target_horizon=run.target_horizon if run is not None else None,
    )


def _detail_response(session: Session, version: NeuralModelVersion) -> NeuralModelDetailResponse:
    item = _list_item(version)
    return NeuralModelDetailResponse(
        **item.model_dump(),
        train_start=version.train_start,
        train_end=version.train_end,
        latent_names=list(version.latent_names),
        gate_result=_gate_result_response(session, version),
    )


@router.get("/api/v1/neural/models", response_model=NeuralModelListResponse)
def list_neural_models(
    session: Session = Depends(get_session),
    status: Optional[str] = Query(None),
):
    """Return neural model versions, newest first."""
    versions = list_neural_model_versions(session, status=status)
    return {
        "models": [_list_item(version) for version in versions],
    }


@router.post("/api/v1/neural/models/train", response_model=NeuralTrainStartResponse)
def start_neural_training(body: NeuralTrainRequest):
    """Enqueue a neural encoder training job on the worker pool."""
    try:
        return neural_jobs.start_training_job(request=body)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get(
    "/api/v1/neural/models/train/{job_id}",
    response_model=NeuralTrainStatusResponse,
)
def get_neural_training_status(job_id: str):
    """Return progress/status for a neural training job."""
    payload = neural_jobs.get_training_status_payload(job_id)
    if payload is None:
        raise HTTPException(
            status_code=404,
            detail=f"Neural training job '{job_id}' not found.",
        )
    return payload


@router.get("/api/v1/neural/models/{model_hash}", response_model=NeuralModelDetailResponse)
def get_neural_model(model_hash: str, session: Session = Depends(get_session)):
    """Return full detail for one neural model version."""
    version = get_neural_model_version(session, model_hash)
    if version is None:
        raise HTTPException(
            status_code=404,
            detail=f"Neural model version '{model_hash}' not found.",
        )
    return _detail_response(session, version)


@router.post(
    "/api/v1/neural/models/{model_hash}/status",
    response_model=NeuralModelDetailResponse,
)
def update_neural_model_status(
    model_hash: str,
    body: NeuralModelStatusUpdateRequest,
    session: Session = Depends(get_session),
):
    """Promote or demote a neural model version's lifecycle status."""
    try:
        promote_neural_model(
            session,
            model_hash=model_hash,
            target_status=body.status.value,
        )
    except NeuralModelNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except IllegalNeuralStatusTransition as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    version = get_neural_model_version(session, model_hash)
    if version is None:
        raise HTTPException(
            status_code=404,
            detail=f"Neural model version '{model_hash}' not found.",
        )
    return _detail_response(session, version)
