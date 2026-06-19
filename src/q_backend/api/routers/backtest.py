from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from q_backend.api import backtest_jobs
from q_backend.api.backtest_jobs import BacktestJobRequest
from q_backend.api.deps import get_session
from q_backend.api.schemas.backtest import (
    BacktestEquityArtifactResponse,
    BacktestResponse,
    BacktestRunDetailResponse,
    BacktestRunListResponse,
    BacktestRunPatchRequest,
    BacktestStartResponse,
    BacktestStatusResponse,
    BacktestTradesArtifactResponse,
)
from q_backend.api.schemas.common import BulkDeleteBacktestsRequest, BulkDeleteResponse
from q_backend.backtesting import run_service as backtest_run_service
from q_backend.storage.lake import read_backtest_result

router = APIRouter(tags=["backtest"])


@router.post("/api/v1/backtest", response_model=BacktestStartResponse)
def start_backtest(request: BacktestJobRequest):
    """Dispatch a backtest to the worker pool and return its run id for polling."""
    run_id = backtest_jobs.start_job(request)
    return {"run_id": run_id, "status": "running"}


@router.get("/api/v1/backtest/{run_id}", response_model=BacktestStatusResponse)
def get_backtest_status(run_id: str):
    """Return the current status of an async backtest run."""
    payload = backtest_jobs.get_status_payload(run_id)
    if payload is None:
        raise HTTPException(
            status_code=404, detail=f"Backtest run '{run_id}' not found."
        )
    return payload


@router.get("/api/v1/backtest/{run_id}/result", response_model=BacktestResponse)
def get_backtest_result(run_id: str):
    """Return the full chart payload for a completed async backtest run."""
    try:
        return read_backtest_result(run_id)
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=404,
            detail=f"Backtest result not ready or not found for run '{run_id}'.",
        ) from exc


@router.get("/api/v1/backtests", response_model=BacktestRunListResponse)
def list_backtests(
    session: Session = Depends(get_session),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    symbol: Optional[str] = None,
    strategy: Optional[str] = None,
    saved_only: Optional[bool] = None,
    sort: Literal["created_at_desc", "pnl_desc", "pnl_asc"] = "created_at_desc",
):
    """Return a paginated list of backtest runs."""
    return backtest_run_service.list_runs(
        session,
        limit=limit,
        offset=offset,
        symbol=symbol,
        strategy=strategy,
        saved_only=saved_only,
        sort=sort,
    )


@router.post("/api/v1/backtests/bulk-delete", response_model=BulkDeleteResponse)
def bulk_delete_backtests(
    body: BulkDeleteBacktestsRequest,
    session: Session = Depends(get_session),
):
    """Delete multiple backtest runs in one request."""
    return backtest_run_service.bulk_delete(session, body)


@router.get("/api/v1/backtests/{run_id}", response_model=BacktestRunDetailResponse)
def get_backtest(run_id: str, session: Session = Depends(get_session)):
    """Return a single backtest run by id."""
    return backtest_run_service.get_run(session, run_id)


@router.patch("/api/v1/backtests/{run_id}", response_model=BacktestRunDetailResponse)
def patch_backtest(
    run_id: str,
    body: BacktestRunPatchRequest,
    session: Session = Depends(get_session),
):
    """Update bookmark state for a backtest run."""
    return backtest_run_service.patch_run(session, run_id, body)


@router.delete("/api/v1/backtests/{run_id}", status_code=204)
def delete_backtest(run_id: str, session: Session = Depends(get_session)):
    """Delete a persisted backtest run from history."""
    backtest_run_service.delete(session, run_id)


@router.get(
    "/api/v1/backtests/{run_id}/artifacts/equity",
    response_model=BacktestEquityArtifactResponse,
)
def get_backtest_equity_artifact(run_id: str):
    """Return the persisted equity curve for a backtest run."""
    return backtest_run_service.read_equity_artifact(run_id)


@router.get(
    "/api/v1/backtests/{run_id}/artifacts/trades",
    response_model=BacktestTradesArtifactResponse,
)
def get_backtest_trades_artifact(run_id: str):
    """Return the persisted closed trades for a backtest run."""
    return backtest_run_service.read_trades_artifact(run_id)
