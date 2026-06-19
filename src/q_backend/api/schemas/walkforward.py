from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel

from q_backend.api.schemas.backtest import EquityArtifactPoint


class WalkForwardStartResponse(BaseModel):
    run_id: str
    status: str


class WalkForwardStatusResponse(BaseModel):
    run_id: str
    status: str
    current_window: int
    total_windows: int
    phase: Optional[Literal["optimizing", "testing"]] = None
    windows_completed: int
    error: Optional[str] = None
    optimization_config: Optional[Dict[str, Any]] = None
    walkforward_config: Optional[Dict[str, Any]] = None
    backtest_config: Optional[Dict[str, Any]] = None


class WalkForwardWindowResultResponse(BaseModel):
    index: int
    train_start: str
    train_end: str
    test_start: str
    test_end: str
    status: str
    best_params: Dict[str, Any] = {}
    is_metrics: Optional[Dict[str, Any]] = None
    oos_metrics: Optional[Dict[str, Any]] = None


class WalkForwardResultsResponse(BaseModel):
    run_id: str
    status: str
    windows: List[WalkForwardWindowResultResponse]
    oos_metrics: Dict[str, Any]
    efficiency: Optional[float] = None
    equity_curve: List[EquityArtifactPoint]
    optimization_config: Optional[Dict[str, Any]] = None
    walkforward_config: Optional[Dict[str, Any]] = None
    lake_paths: Optional[Dict[str, str]] = None


class WalkForwardRunListItem(BaseModel):
    run_id: str
    name: str
    status: str
    symbol: Optional[str] = None
    strategy: Optional[str] = None
    efficiency: Optional[float] = None
    window_count: int = 0
    created_at: datetime


class WalkForwardRunListResponse(BaseModel):
    items: List[WalkForwardRunListItem]
    total: int
    limit: int
    offset: int
