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


class OhlcvAvailableRangeResponse(BaseModel):
    symbol: str
    timeframe: str
    start: str
    end: str
    bar_count: int
