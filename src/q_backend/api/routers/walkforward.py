import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from q_backend.api import walkforward_jobs
from q_backend.api.deps import get_session
from q_backend.api.dependencies import get_market_data_service
from q_backend.api.schemas.backtest import BacktestEquityArtifactResponse
from q_backend.api.schemas.walkforward import (
    WalkForwardResultsResponse,
    WalkForwardRunListItem,
    WalkForwardRunListResponse,
    WalkForwardStartResponse,
    WalkForwardStatusResponse,
)
from q_backend.api.walkforward_jobs import WalkForwardRequest
from q_backend.backtesting.run_service import serialize_equity_artifact
from q_backend.market_data.service import MarketDataService
from q_backend.storage.db.repositories import delete_walkforward_run, list_walkforward_runs
from q_backend.storage.lake import read_walkforward_artifact

logger = logging.getLogger(__name__)

router = APIRouter(tags=["walkforward"])


@router.post("/api/v1/walkforward", response_model=WalkForwardStartResponse)
def start_walkforward(
    body: WalkForwardRequest,
    mds: MarketDataService = Depends(get_market_data_service),
):
    """Launch an asynchronous walk-forward analysis run."""
    try:
        job = walkforward_jobs.start_job(body, market_data_service=mds)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        logger.error("Error starting walk-forward run: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"run_id": job.run_id, "status": job.status}


@router.get(
    "/api/v1/walkforward/{run_id}",
    response_model=WalkForwardStatusResponse,
)
def get_walkforward_status(run_id: str):
    """Return progress/status for a walk-forward run."""
    payload = walkforward_jobs.get_status_payload(run_id)
    if payload is None:
        raise HTTPException(status_code=404, detail=f"Walk-forward run '{run_id}' not found.")
    return payload


@router.get(
    "/api/v1/walkforward/{run_id}/results",
    response_model=WalkForwardResultsResponse,
)
def get_walkforward_results(run_id: str):
    """Return full walk-forward results once the run has finished."""
    job = walkforward_jobs.get_job(run_id)
    if job is not None:
        payload = walkforward_jobs.results_payload(job)
        if payload is None:
            raise HTTPException(
                status_code=409,
                detail=(f"Walk-forward run '{run_id}' has no results yet " f"(status: {job.status})."),
            )
        return payload

    payload = walkforward_jobs.results_payload_from_db(run_id)
    if payload is None:
        persisted_status = walkforward_jobs.get_persisted_run_status(run_id)
        if persisted_status is not None:
            raise HTTPException(
                status_code=409,
                detail=(f"Walk-forward run '{run_id}' has no results yet " f"(status: {persisted_status})."),
            )
        raise HTTPException(status_code=404, detail=f"Walk-forward run '{run_id}' not found.")
    return payload


@router.post(
    "/api/v1/walkforward/{run_id}/cancel",
    response_model=WalkForwardStatusResponse,
)
def cancel_walkforward(run_id: str):
    """Request cancellation of a running walk-forward analysis.

    Cancels the live job if present; otherwise cancels an orphaned run left
    active in the DB by a previous process. A 404 only means the run does not
    exist anywhere.
    """
    walkforward_jobs.request_cancel(run_id)
    payload = walkforward_jobs.get_status_payload(run_id)
    if payload is None:
        raise HTTPException(status_code=404, detail=f"Walk-forward run '{run_id}' not found.")
    return payload


@router.get("/api/v1/walkforwards", response_model=WalkForwardRunListResponse)
def list_walkforwards(
    session: Session = Depends(get_session),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """Return a paginated list of walk-forward runs, newest first."""
    runs, total = list_walkforward_runs(session, limit=limit, offset=offset)
    return {
        "items": [WalkForwardRunListItem(**walkforward_jobs.run_list_item_from_db(run)) for run in runs],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.delete("/api/v1/walkforwards/{run_id}", status_code=204)
def delete_walkforward(run_id: str, session: Session = Depends(get_session)):
    """Delete a persisted walk-forward run and its lake artifacts."""
    try:
        run_uuid = uuid.UUID(run_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=f"Walk-forward run '{run_id}' not found.") from exc

    if not delete_walkforward_run(session, run_uuid):
        raise HTTPException(status_code=404, detail=f"Walk-forward run '{run_id}' not found.")

    walkforward_jobs.evict_run(run_id)
    walkforward_jobs.delete_run_lake_artifacts(run_id)


@router.get(
    "/api/v1/walkforward/{run_id}/artifacts/equity",
    response_model=BacktestEquityArtifactResponse,
)
def get_walkforward_equity_artifact(run_id: str):
    """Return the stitched out-of-sample equity curve for a walk-forward run."""
    try:
        uuid.UUID(run_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=f"Walk-forward run '{run_id}' not found.") from exc

    job = walkforward_jobs.get_job(run_id)
    if job is not None and job.result is not None:
        return {
            "run_id": run_id,
            "points": walkforward_jobs.serialize_equity_points(job.result.oos_equity_curve),
        }

    try:
        df = read_walkforward_artifact(run_id, "oos_equity")
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    return {"run_id": run_id, "points": serialize_equity_artifact(df)}
