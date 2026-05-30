from datetime import datetime
from typing import Optional
from pydantic import BaseModel, Field

class OHLCV(BaseModel):
    """
    Standardized OHLCV candle model.
    """
    time: datetime = Field(..., description="Start time of the candle")
    open: float = Field(..., description="Opening price")
    high: float = Field(..., description="Highest price")
    low: float = Field(..., description="Lowest price")
    close: float = Field(..., description="Closing price")
    tick_volume: int = Field(..., description="Tick volume (number of price updates)")
    spread: Optional[int] = Field(None, description="Spread value")
    real_volume: Optional[int] = Field(None, description="Real traded volume")

    class Config:
        from_attributes = True


class Tick(BaseModel):
    """
    Standardized Tick market data model.
    """
    time: datetime = Field(..., description="Timestamp of the tick")
    bid: float = Field(..., description="Current bid price")
    ask: float = Field(..., description="Current ask price")
    last: Optional[float] = Field(0.0, description="Last traded price")
    volume: Optional[float] = Field(0.0, description="Volume of the last trade")
    flags: Optional[int] = Field(0, description="Tick flags from MetaTrader")
    time_msc: Optional[int] = Field(0, description="Timestamp in milliseconds")

    class Config:
        from_attributes = True
