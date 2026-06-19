from datetime import datetime

from fastapi import APIRouter, HTTPException

from q_backend.api.dependencies import _data_source_payload, market_data_service
from q_backend.api.schemas.system import (
    DataSourceResponse,
    DataSourceUpdateRequest,
    SystemHealthResponse,
)
from q_backend.market_data import local_store
from q_backend.storage.health import storage_status
from q_backend.storage.runtime_config import set_data_source

router = APIRouter(tags=["system"])


@router.get("/")
def read_root():
    return {
        "status": "online",
        "service": "QuantLauncher Backend API",
        "mt5_connected": market_data_service.mt5_connected(),
    }


@router.get("/api/v1/system/health", response_model=SystemHealthResponse)
def get_system_health():
    """
    Exposes platform health telemetry.
    """
    mt5_up = market_data_service.mt5_connected()
    return {
        "status": "healthy" if mt5_up else "degraded",
        "backendVersion": "0.1.0",
        "dataLakeStatus": "online" if mt5_up else "offline",
        "lastSyncAt": datetime.now().isoformat(),
        "storageStatus": storage_status(),
        "mt5_available": mt5_up,
        "active_provider": market_data_service.active_provider(),
        "market_data_root": str(local_store.market_data_root()),
        "market_data_inventory_count": local_store.inventory_count(),
    }


@router.get("/api/v1/system/data-source", response_model=DataSourceResponse)
def get_data_source_setting():
    return _data_source_payload()


@router.put("/api/v1/system/data-source", response_model=DataSourceResponse)
def update_data_source_setting(body: DataSourceUpdateRequest):
    try:
        set_data_source(body.source)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _data_source_payload()
