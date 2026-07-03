"""Executable quote sources for the execution worker hot path."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Callable, Optional, Protocol

from q_backend.execution.brokers.base import ExecutableQuote, QuoteSource

logger = logging.getLogger(__name__)


class TickReader(Protocol):
    def __call__(
        self, symbol: str
    ) -> Optional[tuple[Decimal, Decimal, datetime]]: ...


class SymbolSelector(Protocol):
    def __call__(self, symbol: str) -> bool: ...


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class CallableQuoteSource:
    """Quote source backed by injectable callables (tests and MT5 wiring)."""

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


def quote_source_from_market_data_service(service) -> QuoteSource:
    """Build a quote source from ``MarketDataService``'s MT5 client."""

    def _read_tick(symbol: str) -> Optional[tuple[Decimal, Decimal, datetime]]:
        client = service.mt5_client
        if not client.is_supported():
            return None
        try:
            client._ensure_connected()
        except Exception:  # noqa: BLE001 - no-quote is a handled, fail-closed outcome
            # Best-effort/fail-closed: if the terminal can't be reached we return
            # no quote, so the execution worker simply takes no action this poll
            # and retries next tick. Log so a persistent outage is visible.
            logger.warning(
                "MT5 connection failed while reading tick for %s", symbol,
                exc_info=True,
            )
            return None
        import MetaTrader5 as mt5  # type: ignore

        if not mt5.symbol_select(symbol, True):
            return None
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            return None
        ts = datetime.fromtimestamp(int(tick.time), tz=timezone.utc)
        return Decimal(str(tick.bid)), Decimal(str(tick.ask)), ts

    def _select(symbol: str) -> bool:
        return service.get_symbol_info(symbol) is not None

    return CallableQuoteSource(
        read_tick=_read_tick,
        select_symbol=_select,
        source="mt5",
    )
