from typing import List, Literal, Optional

from pydantic import BaseModel


class InstrumentResponse(BaseModel):
    symbol: str
    name: str
    exchange: str
    assetClass: str


class MarketSnapshotResponse(BaseModel):
    symbol: str
    last: float
    changePct: float
    volume: int
    bid: float = 0.0
    ask: float = 0.0
    spread: float = 0.0
    changeAbs: float = 0.0
    dayOpen: float = 0.0
    dayHigh: float = 0.0
    dayLow: float = 0.0
    prevClose: float = 0.0
    digits: int = 0
    tickTime: Optional[str] = None


class MarketSnapshotsResponse(BaseModel):
    snapshots: List[MarketSnapshotResponse]


class MarketTapeTickResponse(BaseModel):
    timestamp: str
    bid: float
    ask: float
    last: float
    volume: float
    side: Optional[Literal["buy", "sell"]] = None


class MarketTicksResponse(BaseModel):
    ticks: List[MarketTapeTickResponse]


class InstrumentInfoResponse(BaseModel):
    symbol: str
    description: str
    exchange: str
    currencyBase: str
    currencyProfit: str
    digits: int
    point: float
    tickSize: float
    tickValue: float
    contractSize: float
    volumeMin: float
    volumeMax: float
    volumeStep: float
    spreadFloating: bool


class OhlcvBarResponse(BaseModel):
    timestamp: str
    open: float
    high: float
    low: float
    close: float
    volume: int


class TickAvailableRangeResponse(BaseModel):
    symbol: str
    start: str
    end: str


class OhlcvAvailableRangeResponse(BaseModel):
    symbol: str
    timeframe: str
    start: str
    end: str
    bar_count: int


class IndicatorParameterSpecResponse(BaseModel):
    name: str
    type: str
    default: float | int | str | None
    minimum: float | None = None
    maximum: float | None = None
    choices: List[str] | None = None


class IndicatorOutputSpecResponse(BaseModel):
    key: str
    label: str
    pane: str


class IndicatorCatalogEntryResponse(BaseModel):
    id: str
    label: str
    category: str
    pane: str
    unit: str | None = None
    requiredData: str
    parameters: List[IndicatorParameterSpecResponse]
    outputs: List[IndicatorOutputSpecResponse]


class IndicatorCatalogResponse(BaseModel):
    indicators: List[IndicatorCatalogEntryResponse]


class MarketIndicatorRequest(BaseModel):
    id: str
    parameters: dict[str, float | int | str] = {}


class MarketIndicatorSeriesRequest(BaseModel):
    timeframe: str
    start: str
    end: str
    indicators: List[MarketIndicatorRequest]


class MarketIndicatorSeriesOutput(BaseModel):
    key: str
    pane: str
    unit: str | None = None
    values: List[float | None]
    unavailableReason: str | None = None


class MarketIndicatorSeriesResponse(BaseModel):
    timestamps: List[str]
    series: List[MarketIndicatorSeriesOutput]
