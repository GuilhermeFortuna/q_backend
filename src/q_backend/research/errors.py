"""Research library errors."""

from __future__ import annotations


class NoMarketDataError(ValueError):
    """Raised when a bars query returns no rows after normalization and filtering."""

    def __init__(
        self,
        *,
        symbol: str,
        timeframe: str,
        source: str,
        start: str,
        end: str,
    ) -> None:
        self.symbol = symbol
        self.timeframe = timeframe
        self.source = source
        self.start = start
        self.end = end
        super().__init__(f"No market data for {symbol} {timeframe} from {source} between {start} and {end}")
