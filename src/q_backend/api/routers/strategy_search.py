import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from q_backend.api import strategy_search_jobs
from q_backend.api.deps import get_session
from q_backend.api.dependencies import get_market_data_service
from q_backend.api.schemas.strategy_search import (
    StrategySearchCandidateEquityArtifactResponse,
    StrategySearchCandidateGenomeResponse,
    StrategySearchResultsResponse,
    StrategySearchRunListItem,
    StrategySearchRunListResponse,
    StrategySearchStartResponse,
    StrategySearchStatusResponse,
)
from q_backend.backtesting.run_service import serialize_equity_artifact
from q_backend.market_data.service import MarketDataService
from q_backend.optimization.strategy_search import StrategySearchConfig
from q_backend.observability.sentry import trading_context
from q_backend.storage.db.repositories import (
    delete_strategy_search_run,
    list_strategy_search_runs,
)
from q_backend.storage.lake import read_strategy_search_candidate_artifact

logger = logging.getLogger(__name__)

router = APIRouter(tags=["strategy-search"])


@router.post("/api/v1/strategy-search", response_model=StrategySearchStartResponse)
def start_strategy_search(
    body: StrategySearchConfig,
    mds: MarketDataService = Depends(get_market_data_service),
):
    """Launch an asynchronous strategy search run."""
    try:
        with trading_context(symbol=body.backtest.symbol):
            job = strategy_search_jobs.start_job(body, market_data_service=mds)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        logger.error("Error starting strategy search run: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"run_id": job.run_id, "status": job.status}


@router.get(
    "/api/v1/strategy-search/{run_id}",
    response_model=StrategySearchStatusResponse,
)
def get_strategy_search_status(run_id: str):
    """Return progress/status for a strategy search run."""
    payload = strategy_search_jobs.get_status_payload(run_id)
    if payload is None:
        raise HTTPException(
            status_code=404, detail=f"Strategy search run '{run_id}' not found."
        )
    return payload


@router.get(
    "/api/v1/strategy-search/{run_id}/results",
    response_model=StrategySearchResultsResponse,
)
def get_strategy_search_results(run_id: str):
    """Return full strategy search results once the run has finished."""
    job = strategy_search_jobs.get_job(run_id)
    if job is not None:
        payload = strategy_search_jobs.results_payload(job)
        if payload is None:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Strategy search run '{run_id}' has no results yet "
                    f"(status: {job.status})."
                ),
            )
        return payload

    payload = strategy_search_jobs.results_payload_from_db(run_id)
    if payload is None:
        persisted_status = strategy_search_jobs.get_persisted_run_status(run_id)
        if persisted_status is not None:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Strategy search run '{run_id}' has no results yet "
                    f"(status: {persisted_status})."
                ),
            )
        raise HTTPException(
            status_code=404, detail=f"Strategy search run '{run_id}' not found."
        )
    return payload


@router.post(
    "/api/v1/strategy-search/{run_id}/cancel",
    response_model=StrategySearchStatusResponse,
)
def cancel_strategy_search(run_id: str):
    """Request cancellation of a running strategy search.

    Cancels the live job if present; otherwise cancels an orphaned run left
    active in the DB by a previous process. The response reflects the current
    status, so a 404 only means the run does not exist anywhere.
    """
    strategy_search_jobs.request_cancel(run_id)
    payload = strategy_search_jobs.get_status_payload(run_id)
    if payload is None:
        raise HTTPException(
            status_code=404, detail=f"Strategy search run '{run_id}' not found."
        )
    return payload


@router.get("/api/v1/strategy-searches", response_model=StrategySearchRunListResponse)
def list_strategy_searches(
    session: Session = Depends(get_session),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """Return a paginated list of strategy search runs, newest first."""
    runs, total = list_strategy_search_runs(session, limit=limit, offset=offset)
    return {
        "items": [
            StrategySearchRunListItem(
                **strategy_search_jobs.run_list_item_from_db(run)
            )
            for run in runs
        ],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.delete("/api/v1/strategy-searches/{run_id}", status_code=204)
def delete_strategy_search(run_id: str, session: Session = Depends(get_session)):
    """Delete a persisted strategy search run and its lake artifacts."""
    try:
        run_uuid = uuid.UUID(run_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=404, detail=f"Strategy search run '{run_id}' not found."
        ) from exc

    if not delete_strategy_search_run(session, run_uuid):
        raise HTTPException(
            status_code=404, detail=f"Strategy search run '{run_id}' not found."
        )

    strategy_search_jobs.evict_run(run_id)
    strategy_search_jobs.delete_run_lake_artifacts(run_id)


@router.get(
    "/api/v1/strategy-search/{run_id}/candidates/{candidate_id}/artifacts/equity",
    response_model=StrategySearchCandidateEquityArtifactResponse,
)
def get_strategy_search_candidate_equity_artifact(run_id: str, candidate_id: str):
    """Return stitched out-of-sample equity points for one search candidate."""
    try:
        uuid.UUID(run_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=404, detail=f"Strategy search run '{run_id}' not found."
        ) from exc

    if not strategy_search_jobs.candidate_exists_in_run(run_id, candidate_id):
        raise HTTPException(
            status_code=404,
            detail=(
                f"Candidate '{candidate_id}' not found in strategy search run "
                f"'{run_id}'."
            ),
        )

    job = strategy_search_jobs.get_job(run_id)
    if job is not None and job.result is not None:
        candidate = next(
            (
                item
                for item in job.result.candidates
                if item.candidate_id == candidate_id
            ),
            None,
        )
        if candidate is not None and candidate.oos_equity_curve is not None:
            return {
                "run_id": run_id,
                "candidate_id": candidate_id,
                "points": strategy_search_jobs.serialize_equity_points(
                    candidate.oos_equity_curve
                ),
            }

    try:
        df = read_strategy_search_candidate_artifact(
            run_id, candidate_id, "oos_equity"
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    return {
        "run_id": run_id,
        "candidate_id": candidate_id,
        "points": serialize_equity_artifact(df),
    }


@router.get(
    "/api/v1/strategy-search/{run_id}/candidates/{candidate_id}/genome",
    response_model=StrategySearchCandidateGenomeResponse,
)
def get_strategy_search_candidate_genome(run_id: str, candidate_id: str):
    """Return the stored genome document for a genetic search candidate."""
    try:
        uuid.UUID(run_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=404, detail=f"Strategy search run '{run_id}' not found."
        ) from exc

    if not strategy_search_jobs.candidate_exists_in_run(run_id, candidate_id):
        raise HTTPException(
            status_code=404,
            detail=(
                f"Candidate '{candidate_id}' not found in strategy search run "
                f"'{run_id}'."
            ),
        )

    genome = strategy_search_jobs.get_candidate_genome(run_id, candidate_id)
    if genome is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"Genome not available for candidate '{candidate_id}' "
                f"in strategy search run '{run_id}'."
            ),
        )

    return {
        "run_id": run_id,
        "candidate_id": candidate_id,
        "genome": genome,
    }
