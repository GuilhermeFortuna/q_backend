from typing import Literal, Optional

from pydantic import BaseModel


class StorageServiceStatus(BaseModel):
    status: Literal["ok", "error"]
    error: Optional[str] = None


class StorageStatusResponse(BaseModel):
    postgres: StorageServiceStatus
    redis: StorageServiceStatus


class SystemHealthResponse(BaseModel):
    status: str
    backendVersion: str
    dataLakeStatus: str
    lastSyncAt: str
    storageStatus: StorageStatusResponse
    mt5_available: bool
    active_provider: Literal["mt5", "local"]
    market_data_root: str
    market_data_inventory_count: int


class DataSourceResponse(BaseModel):
    source: Literal["auto", "mt5", "local"]
    mt5_available: bool
    active_provider: Literal["mt5", "local"]


class DataSourceUpdateRequest(BaseModel):
    source: Literal["auto", "mt5", "local"]
