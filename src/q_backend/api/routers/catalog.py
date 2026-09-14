"""FastAPI router for the lake dataset catalog API."""

from __future__ import annotations

import dataclasses
import uuid

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy.exc import OperationalError

from q_backend.api.schemas.catalog import DatasetListResponse, DatasetManifestResponse
from q_backend.api.schemas.stream import ErrorResponse
from q_backend.market_data.catalog.repository import get_dataset, list_current_datasets, to_manifest
from q_backend.market_data.catalog.service import get_lake_catalog

router = APIRouter(prefix="/api/v1/catalog", tags=["catalog"])


@router.get(
    "/datasets",
    response_model=DatasetListResponse,
    responses={503: {"model": ErrorResponse}},
)
def list_datasets(
    kind: str | None = None,
    symbol: str | None = None,
    timeframe: str | None = None,
):
    catalog = get_lake_catalog()
    try:
        with catalog.session_factory() as session:
            datasets = list_current_datasets(
                session,
                kind=kind,
                symbol=symbol,
                timeframe=timeframe,
            )
            manifests = [DatasetManifestResponse.model_validate(dataclasses.asdict(to_manifest(d))) for d in datasets]
            return DatasetListResponse(
                root=str(catalog.root.resolve()),
                datasets=manifests,
            )
    except OperationalError as exc:
        return JSONResponse(
            status_code=503,
            content=ErrorResponse(
                message=f"Catalog database unavailable: {exc}",
                code="catalog_unavailable",
            ).model_dump(),
        )


@router.get(
    "/datasets/{dataset_id}",
    response_model=DatasetManifestResponse,
    responses={404: {"model": ErrorResponse}, 503: {"model": ErrorResponse}},
)
def get_dataset_by_id(dataset_id: str):
    try:
        uid = uuid.UUID(dataset_id)
    except ValueError:
        return JSONResponse(
            status_code=404,
            content=ErrorResponse(
                message=f"Dataset '{dataset_id}' not found",
                code="dataset_not_found",
            ).model_dump(),
        )

    catalog = get_lake_catalog()
    try:
        with catalog.session_factory() as session:
            dataset = get_dataset(session, uid)
            if dataset is None:
                return JSONResponse(
                    status_code=404,
                    content=ErrorResponse(
                        message=f"Dataset '{dataset_id}' not found",
                        code="dataset_not_found",
                    ).model_dump(),
                )
            return DatasetManifestResponse.model_validate(dataclasses.asdict(to_manifest(dataset)))
    except OperationalError as exc:
        return JSONResponse(
            status_code=503,
            content=ErrorResponse(
                message=f"Catalog database unavailable: {exc}",
                code="catalog_unavailable",
            ).model_dump(),
        )
