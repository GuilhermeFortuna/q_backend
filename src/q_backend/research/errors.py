"""Research library errors and warnings."""

from __future__ import annotations


class AdjustedSeriesWarning(UserWarning):
    """
    Warning emitted when loaded prices sit off the instrument's official tick grid.

    Typically occurs when loading proportionally adjusted continuous futures series
    (e.g., ``WIN$`` or ``WDO$``). For point-based PnL backtesting, use unadjusted
    (``$N``) or difference-adjusted (``$D``) series instead.
    """


class NoMarketDataError(ValueError):
    """
    Exception raised when a query returns no bars or ticks in the requested interval.
    """

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
