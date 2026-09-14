from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from q_backend.api import storage_jobs
from q_backend.api.dependencies import get_market_data_service
from q_backend.api.schemas.storage import (
    StorageDeleteResponse,
    StorageIngestStartResponse,
    StorageIngestStatusResponse,
    StorageInventoryResponse,
)
from q_backend.api.schemas.stream import ErrorResponse
from q_backend.api.storage_jobs import IngestJobRequest
from q_backend.market_data import local_store
from q_backend.market_data.service import MarketDataService

router = APIRouter(tags=["storage"])


@router.get(
    "/api/v1/storage/inventory",
    response_model=StorageInventoryResponse,
    responses={503: {"model": ErrorResponse}},
)
def get_storage_inventory():
    try:
        items = local_store.list_inventory()
    except OperationalError as exc:
        return JSONResponse(
            status_code=503,
            content=ErrorResponse(
                message=f"Catalog database unavailable: {exc}",
                code="catalog_unavailable",
            ).model_dump(),
        )
    return {
        "root": str(local_store.market_data_root()),
        "items": items,
    }


@router.post(
    "/api/v1/storage/ingest",
    response_model=StorageIngestStartResponse,
    responses={503: {"model": ErrorResponse}},
)
def start_storage_ingest(
    request: IngestJobRequest,
    mds: MarketDataService = Depends(get_market_data_service),
):
    catalog = local_store.get_lake_catalog()
    try:
        with catalog.session_factory() as session:
            session.execute(text("SELECT 1"))
    except OperationalError as exc:
        return JSONResponse(
            status_code=503,
            content=ErrorResponse(
                message=f"Catalog database unavailable: {exc}",
                code="catalog_unavailable",
            ).model_dump(),
        )

    try:
        mds.acquisition_provider()
    except ConnectionError as exc:
        raise HTTPException(
            status_code=503,
            detail=("Ingestion requires a reachable acquisition provider " f"(native MT5 or remote gateway): {exc}"),
        ) from exc
    try:
        if request.kind == "bars":
            storage_jobs.validate_timeframes(request.timeframes)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    job_id = storage_jobs.start_job(request)
    return {"job_id": job_id, "status": "queued"}


@router.get(
    "/api/v1/storage/ingest/{job_id}",
    response_model=StorageIngestStatusResponse,
)
def get_storage_ingest_status(job_id: str):
    payload = storage_jobs.get_status_payload(job_id)
    if payload is None:
        raise HTTPException(status_code=404, detail=f"Storage ingest job '{job_id}' not found.")
    return payload


@router.delete(
    "/api/v1/storage/{symbol}/{timeframe}",
    response_model=StorageDeleteResponse,
    responses={503: {"model": ErrorResponse}},
)
def delete_storage_series(symbol: str, timeframe: str):
    try:
        local_store.delete_ohlcv(symbol.upper(), timeframe.upper())
    except OperationalError as exc:
        return JSONResponse(
            status_code=503,
            content=ErrorResponse(
                message=f"Catalog database unavailable: {exc}",
                code="catalog_unavailable",
            ).model_dump(),
        )
    return {
        "deleted": True,
        "symbol": symbol.upper(),
        "timeframe": timeframe.upper(),
    }
