"""Thin MT5 runtime wrapper for the execution worker's serialized owner thread."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional, Protocol, runtime_checkable


@runtime_checkable
class Mt5Runtime(Protocol):
    def is_stub(self) -> bool: ...

    def initialize(self) -> bool: ...

    def shutdown(self) -> None: ...

    def last_error(self) -> tuple[int, str]: ...

    def account_info(self) -> Any: ...

    def symbol_select(self, symbol: str, enable: bool) -> bool: ...

    def symbol_info(self, symbol: str) -> Any: ...

    def symbol_info_tick(self, symbol: str) -> Any: ...

    def order_check(self, request: dict[str, Any]) -> Any: ...

    def order_send(self, request: dict[str, Any]) -> Any: ...

    def orders_get(self, *, ticket: Optional[int] = None) -> Any: ...

    def positions_get(self, *, symbol: Optional[str] = None) -> Any: ...

    def history_orders_get(
        self,
        start: datetime,
        end: datetime,
        *,
        group: str = "",
    ) -> Any: ...

    def history_deals_get(
        self,
        start: datetime,
        end: datetime,
        *,
        group: str = "",
    ) -> Any: ...


class DefaultMt5Runtime:
    """Delegates to the installed MetaTrader5 package under a caller-owned lock."""

    def __init__(self) -> None:
        try:
            import MetaTrader5 as mt5
        except Exception as exc:  # pragma: no cover - platform import
            raise RuntimeError("MetaTrader5 package is not available") from exc
        self._mt5 = mt5

    def is_stub(self) -> bool:
        return bool(getattr(self._mt5, "IS_STUB", False))

    def initialize(self) -> bool:
        return bool(self._mt5.initialize())

    def shutdown(self) -> None:
        self._mt5.shutdown()

    def last_error(self) -> tuple[int, str]:
        return self._mt5.last_error()

    def account_info(self) -> Any:
        return self._mt5.account_info()

    def symbol_select(self, symbol: str, enable: bool) -> bool:
        return bool(self._mt5.symbol_select(symbol, enable))

    def symbol_info(self, symbol: str) -> Any:
        return self._mt5.symbol_info(symbol)

    def symbol_info_tick(self, symbol: str) -> Any:
        return self._mt5.symbol_info_tick(symbol)

    def order_check(self, request: dict[str, Any]) -> Any:
        return self._mt5.order_check(request)

    def order_send(self, request: dict[str, Any]) -> Any:
        return self._mt5.order_send(request)

    def orders_get(self, *, ticket: Optional[int] = None) -> Any:
        if ticket is not None:
            return self._mt5.orders_get(ticket=ticket)
        return self._mt5.orders_get()

    def positions_get(self, *, symbol: Optional[str] = None) -> Any:
        if symbol is not None:
            return self._mt5.positions_get(symbol=symbol)
        return self._mt5.positions_get()

    def history_orders_get(
        self,
        start: datetime,
        end: datetime,
        *,
        group: str = "",
    ) -> Any:
        return self._mt5.history_orders_get(start, end, group=group)

    def history_deals_get(
        self,
        start: datetime,
        end: datetime,
        *,
        group: str = "",
    ) -> Any:
        return self._mt5.history_deals_get(start, end, group=group)
