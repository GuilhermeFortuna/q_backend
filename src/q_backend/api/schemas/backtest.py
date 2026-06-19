from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel

from q_backend.api.schemas.market import OhlcvBarResponse
from q_backend.backtesting.costs import TransactionCostConfig
from q_backend.backtesting.position_sizing import PositionSizingConfig


class BacktestRequest(BaseModel):
    symbol: str
    timeframe: str = "D1"
    start: Optional[datetime] = None
    end: Optional[datetime] = None
    initial_capital: float = 100000.0
    point_value: float = 1.0
    strategy: str = "MACrossover"
    strategy_params: Dict[str, Any] = {}
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


class BacktestStartResponse(BaseModel):
    run_id: str
    status: str


class BacktestStatusResponse(BaseModel):
    run_id: str
    status: str
    error: Optional[str] = None


class BacktestRunListItem(BaseModel):
    run_id: str
    symbol: str
    strategy: str
    timeframe: str
    status: str
    created_at: datetime
    is_saved: bool = False
    summary: Optional[Dict[str, Any]] = None


class BacktestRunListResponse(BaseModel):
    items: List[BacktestRunListItem]
    total: int
    limit: int
    offset: int


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
