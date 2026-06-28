from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel


class StorageInventoryItem(BaseModel):
    symbol: str
    kind: Literal["bars", "ticks"] = "bars"
    # Ticks are not partitioned by timeframe, so the field is absent for them.
    timeframe: Optional[str] = None
    start: str
    end: str
    rows: int
    bytes: int
    updated_at: str


class StorageInventoryResponse(BaseModel):
    root: str
    items: List[StorageInventoryItem]


class StorageIngestStartResponse(BaseModel):
    job_id: str
    status: Literal["queued"]


class StorageIngestStatusResponse(BaseModel):
    job_id: str
    status: Literal["queued", "running", "completed", "failed"]
    progress: float
    detail: str
    results: Optional[List[Dict[str, Any]]] = None
    error: Optional[str] = None


class StorageDeleteResponse(BaseModel):
    deleted: bool
    symbol: str
    timeframe: str
