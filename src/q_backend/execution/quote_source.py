"""Executable quote sources for the execution worker hot path."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Callable, Optional, Protocol

from q_backend.execution.brokers.base import ExecutableQuote, QuoteSource
from q_backend.execution.edge_client import EdgeClient, EdgeUnavailable

logger = logging.getLogger(__name__)


class TickReader(Protocol):
    def __call__(self, symbol: str) -> Optional[tuple[Decimal, Decimal, datetime]]: ...


class SymbolSelector(Protocol):
    def __call__(self, symbol: str) -> bool: ...


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class CallableQuoteSource:
    """Quote source backed by injectable callables (tests)."""

    def __init__(
        self,
        *,
        read_tick: TickReader,
        select_symbol: Optional[SymbolSelector] = None,
        source: str = "callable",
    ) -> None:
        self._read_tick = read_tick
        self._select_symbol = select_symbol
        self._source = source

    def get_quote(self, symbol: str) -> Optional[ExecutableQuote]:
        if self._select_symbol is not None and not self._select_symbol(symbol):
            return None
        tick = self._read_tick(symbol)
        if tick is None:
            return None
        bid, ask, timestamp = tick
        return ExecutableQuote(
            symbol=symbol,
            bid=bid,
            ask=ask,
            timestamp=_as_utc(timestamp),
            source=self._source,
        )


class EdgeQuoteSource:
    """Quote source backed by the execution edge."""

    def __init__(
        self,
        client: EdgeClient,
        *,
        clock: Callable[[], datetime],
        source: str = "edge",
    ) -> None:
        self._client = client
        self._clock = clock
        self._source = source

    def get_quote(self, symbol: str) -> Optional[ExecutableQuote]:
        try:
            quote = self._client.quote(symbol)
        except EdgeUnavailable:
            logger.debug("edge quote unavailable for %s", symbol, exc_info=True)
            return None
        timestamp = _as_utc(self._clock()) - timedelta(milliseconds=int(quote.age_ms))
        return ExecutableQuote(
            symbol=quote.symbol,
            bid=Decimal(str(quote.bid)),
            ask=Decimal(str(quote.ask)),
            timestamp=timestamp,
            source=self._source,
        )
