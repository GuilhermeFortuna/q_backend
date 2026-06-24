import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from q_backend.api import optimization_jobs
from q_backend.api.deps import get_session
from q_backend.api.dependencies import get_market_data_service
from q_backend.api.schemas.common import BulkDeleteOptimizationsRequest, BulkDeleteResponse
from q_backend.api.schemas.optimization import (
    OptimizationAnalyticsResponse,
    OptimizationResultsResponse,
    OptimizationStartResponse,
    OptimizationStatusResponse,
    OptimizationStudyListItem,
    OptimizationStudyListResponse,
)
from q_backend.market_data.service import MarketDataService
from q_backend.optimization import OptimizationConfig
from q_backend.storage.db.repositories import (
    delete_optimization_studies,
    delete_optimization_study,
    list_optimization_studies,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["optimization"])


@router.post("/api/v1/optimize", response_model=OptimizationStartResponse)
def start_optimization(
    config: OptimizationConfig,
    mds: MarketDataService = Depends(get_market_data_service),
):
    """
    Launch an asynchronous Optuna optimization study and return its id.

    The run executes on a background worker; poll the status endpoint for
    progress and fetch results once the study is done.
    """
    try:
        job = optimization_jobs.start_job(config, market_data_service=mds)
    except Exception as exc:
        logger.error("Error starting optimization: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"study_id": job.study_id, "status": job.status}


@router.get(
    "/api/v1/optimize/{study_id}",
    response_model=OptimizationStatusResponse,
)
def get_optimization_status(study_id: str):
    """Return progress/status for an optimization study."""
    payload = optimization_jobs.get_status_payload(study_id)
    if payload is None:
        raise HTTPException(status_code=404, detail=f"Study '{study_id}' not found.")
    return payload


@router.get(
    "/api/v1/optimize/{study_id}/results",
    response_model=OptimizationResultsResponse,
)
def get_optimization_results(study_id: str):
    """Return full study results once the optimization has finished."""
    job = optimization_jobs.get_job(study_id)
    if job is not None:
        payload = optimization_jobs.results_payload(job)
        if payload is None:
            raise HTTPException(
                status_code=409,
                detail=f"Study '{study_id}' has no results yet (status: {job.status}).",
            )
        return payload

    payload = optimization_jobs.results_payload_from_db(study_id)
    if payload is None:
        persisted_status = optimization_jobs.get_persisted_study_status(study_id)
        if persisted_status is not None:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Study '{study_id}' has no results yet "
                    f"(status: {persisted_status})."
                ),
            )
        raise HTTPException(status_code=404, detail=f"Study '{study_id}' not found.")
    return payload


@router.get(
    "/api/v1/optimize/{study_id}/analytics",
    response_model=OptimizationAnalyticsResponse,
)
def get_optimization_analytics(study_id: str):
    """Return state-aware optimization analytics for chart rendering."""
    job = optimization_jobs.get_job(study_id)
    if job is not None:
        payload = optimization_jobs.analytics_payload(job)
        if payload is not None:
            return payload

    payload = optimization_jobs.analytics_payload_from_db(study_id)
    if payload is None:
        raise HTTPException(status_code=404, detail=f"Study '{study_id}' not found.")
    return payload


@router.get("/api/v1/optimizations", response_model=OptimizationStudyListResponse)
def list_optimizations(
    session: Session = Depends(get_session),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """Return a paginated list of optimization studies, newest first."""
    studies, total = list_optimization_studies(
        session, limit=limit, offset=offset
    )
    return {
        "items": [
            OptimizationStudyListItem(**optimization_jobs.study_list_item_from_db(study))
            for study in studies
        ],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.post("/api/v1/optimizations/bulk-delete", response_model=BulkDeleteResponse)
def bulk_delete_optimizations(
    body: BulkDeleteOptimizationsRequest,
    session: Session = Depends(get_session),
):
    """Delete multiple optimization studies in one request."""
    parsed_ids: list[uuid.UUID] = []
    not_found: list[str] = []
    for study_id in body.study_ids:
        try:
            parsed_ids.append(uuid.UUID(study_id))
        except ValueError:
            not_found.append(study_id)

    deleted_count, missing_ids = delete_optimization_studies(session, parsed_ids)
    not_found.extend(str(study_id) for study_id in missing_ids)
    missing_set = set(missing_ids)
    for parsed_id in parsed_ids:
        if parsed_id not in missing_set:
            optimization_jobs.evict_study(str(parsed_id))
    return {"deleted": deleted_count, "not_found": not_found}


@router.delete("/api/v1/optimizations/{study_id}", status_code=204)
def delete_optimization(study_id: str, session: Session = Depends(get_session)):
    """Delete a persisted optimization study from history."""
    try:
        study_uuid = uuid.UUID(study_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=f"Study '{study_id}' not found.") from exc

    if not delete_optimization_study(session, study_uuid):
        raise HTTPException(status_code=404, detail=f"Study '{study_id}' not found.")

    optimization_jobs.evict_study(study_id)


@router.post(
    "/api/v1/optimize/{study_id}/cancel",
    response_model=OptimizationStatusResponse,
)
def cancel_optimization(study_id: str):
    """Request cancellation of a running optimization study.

    Cancels the live job if present; otherwise cancels an orphaned study left
    active in the DB by a previous process. A 404 only means the study does not
    exist anywhere.
    """
    optimization_jobs.request_cancel(study_id)
    payload = optimization_jobs.get_status_payload(study_id)
    if payload is None:
        raise HTTPException(status_code=404, detail=f"Study '{study_id}' not found.")
    return payload
