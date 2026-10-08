from datetime import datetime
from typing import Any, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, Field

from q_backend.api.schemas.market import OhlcvBarResponse
from q_backend.backtesting.costs import TransactionCostConfig
from q_backend.backtesting.entry_models import EntryInstance, EntryManagerConfig
from q_backend.backtesting.position_sizing import PositionSizingConfig

# Re-exported for API consumers importing from this module.
__all__ = [
    "EntryInstance",
    "EntryManagerConfig",
    "BacktestRequest",
    "MLFilterConfig",
    "ChartIndicatorSeries",
    "BacktestResponse",
    "BacktestStartResponse",
    "BacktestStatusResponse",
    "BacktestRunListItem",
]


class MLFilterConfig(BaseModel):
    """Pinned Q-086 model version and acceptance threshold for an entry filter."""

    model_version_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    threshold: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)


class BacktestRequest(BaseModel):
    symbol: str
    timeframe: str = "D1"
    start: Optional[datetime] = None
    end: Optional[datetime] = None
    initial_capital: float = 100000.0
    point_value: float = 1.0
    strategy: str = "MACrossover"
    strategy_params: Dict[str, Any] = {}
    entries: Optional[List[EntryInstance]] = None
    # Omitted from dumped/persisted configs when unset so existing request shapes are unchanged.
    ml_filter: Optional[MLFilterConfig] = Field(default=None, exclude_if=lambda value: value is None)
    entry_manager: EntryManagerConfig = Field(default_factory=EntryManagerConfig)
    exit_params: Dict[str, Any] = {}
    position_sizing: Optional[PositionSizingConfig] = None
    costs: Optional[TransactionCostConfig] = None
    engine: Literal["candle", "tick"] = "candle"
    display_timeframe: str = "M1"
    tick_flags: Optional[str] = None
    day_trade: bool = False
    day_trade_start_time: str = "09:00"
    day_trade_end_time: str = "16:00"
    day_trade_close_time: str = "17:00"


class ChartIndicatorSeries(BaseModel):
    key: str
    label: str
    pane: Literal["price", "oscillator"]
    color: Optional[str] = None
    values: List[Optional[float]]


class BacktestResponse(BaseModel):
    metrics: Dict[str, Any]
    trades: List[Dict[str, Any]]
    bars: List[OhlcvBarResponse]
    indicators: List[ChartIndicatorSeries]
    run_id: Optional[str] = None
    ml_filter: Optional[MLFilterConfig] = Field(default=None, exclude_if=lambda value: value is None)
    ml_filter_summary: Optional[Dict[str, Any]] = Field(default=None, exclude_if=lambda value: value is None)


class BacktestStartResponse(BaseModel):
    run_id: str
    status: str


class BacktestStatusResponse(BaseModel):
    run_id: str
    status: str
    error: Optional[str] = None


BacktestOrigin = Literal["stack", "script"]


class BacktestProvenance(BaseModel):
    script: Optional[str] = None
    strategy_class: Optional[str] = None
    strategy_source: Optional[str] = None
    git_revision: Optional[str] = None
    git_dirty: Optional[bool] = None


class BacktestImportBar(BaseModel):
    """A bar as a script produces it: epoch seconds or the ISO-8601 string the result endpoint serves."""

    timestamp: Union[int, str]
    open: float
    high: float
    low: float
    close: float
    volume: int


class BacktestImportResult(BacktestResponse):
    bars: List[BacktestImportBar]


class BacktestImportRequest(BaseModel):
    config: BacktestRequest
    result: BacktestImportResult
    provenance: BacktestProvenance


class BacktestImportResponse(BaseModel):
    run_id: str


class BacktestRunListItem(BaseModel):
    run_id: str
    symbol: str
    strategy: str
    timeframe: str
    status: str
    created_at: datetime
    is_saved: bool = False
    origin: BacktestOrigin = "stack"
    summary: Optional[Dict[str, Any]] = None


class BacktestRunListResponse(BaseModel):
    items: List[BacktestRunListItem]
    total: int
    limit: int
    offset: int


class MLFilterReference(BaseModel):
    """A saved run's pinned ML filter and whether that exact version is still usable."""

    model_version_id: str
    threshold: float
    available: bool


class BacktestRunDetailResponse(BaseModel):
    run_id: str
    symbol: str
    strategy: str
    timeframe: str
    status: str
    config: Dict[str, Any]
    result_summary: Optional[Dict[str, Any]] = None
    error_message: Optional[str] = None
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    created_at: datetime
    is_saved: bool = False
    origin: BacktestOrigin = "stack"
    provenance: Optional[BacktestProvenance] = None
    ml_filter: Optional[MLFilterReference] = Field(default=None, exclude_if=lambda value: value is None)


class BacktestRunPatchRequest(BaseModel):
    is_saved: bool


class EquityArtifactPoint(BaseModel):
    time: str
    equity: float


class BacktestEquityArtifactResponse(BaseModel):
    run_id: str
    points: List[EquityArtifactPoint]


class BacktestTradesArtifactResponse(BaseModel):
    run_id: str
    trades: List[Dict[str, Any]]
