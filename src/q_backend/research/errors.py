"""Research library errors and warnings."""

from __future__ import annotations


class AdjustedSeriesWarning(UserWarning):
    """Loaded OHLCV prices sit off the instrument tick grid (often a proportionally adjusted series)."""


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
        hint: str = "",
    ) -> None:
        self.symbol = symbol
        self.timeframe = timeframe
        self.source = source
        self.start = start
        self.end = end
        message = f"No market data for {symbol} {timeframe} from {source} between {start} and {end}"
        super().__init__(f"{message}. {hint}" if hint else message)
