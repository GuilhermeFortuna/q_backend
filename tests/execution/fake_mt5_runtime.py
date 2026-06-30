"""Deterministic fake MT5 runtime for live-broker contract tests."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional
from uuid import UUID

from q_backend.execution.brokers.mt5_constants import Mt5OrderFilling, Mt5Retcode, Mt5SymbolFilling


@dataclass
class FakeAccount:
    login: int = 12345678
    trade_allowed: bool = True


@dataclass
class FakeSymbolInfo:
    volume_step: float = 1.0
    volume_min: float = 1.0
    volume_max: float = 100.0
    digits: int = 2
    filling_mode: int = int(Mt5SymbolFilling.IOC)
    trade_mode: int = 4


@dataclass
class FakeTick:
    bid: float = 100.0
    ask: float = 100.5
    time: int = 1_700_000_000


@dataclass
class FakeCheckResult:
    retcode: int = int(Mt5Retcode.DONE)


@dataclass
class FakeSendResult:
    retcode: int = int(Mt5Retcode.DONE)
    order: int = 9001
    deal: int = 0


@dataclass
class FakeDeal:
    ticket: int
    magic: int
    comment: str
    volume: float
    price: float
    time: int


@dataclass
class FakeMt5Runtime:
    account: FakeAccount = field(default_factory=FakeAccount)
    symbols: dict[str, FakeSymbolInfo] = field(default_factory=dict)
    ticks: dict[str, FakeTick] = field(default_factory=dict)
    check_retcode: int = int(Mt5Retcode.DONE)
    send_retcode: int = int(Mt5Retcode.DONE)
    send_result: Optional[FakeSendResult] = None
    order_check_raises: Optional[Exception] = None
    order_send_response: Any = None
    deals: list[FakeDeal] = field(default_factory=list)
    orders: list[dict[str, Any]] = field(default_factory=list)
    last_error_code: int = 0
    last_error_desc: str = ""
    on_order_send: Optional[Callable[[dict[str, Any]], Any]] = None
    send_returns_none: bool = False
    now_fn: Callable[[], datetime] = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )

    def _now_ts(self) -> int:
        return int(self.now_fn().timestamp())

    def is_stub(self) -> bool:
        return False

    def initialize(self) -> bool:
        return True

    def shutdown(self) -> None:
        return None

    def last_error(self) -> tuple[int, str]:
        return self.last_error_code, self.last_error_desc

    def account_info(self) -> FakeAccount:
        return self.account

    def symbol_select(self, symbol: str, enable: bool) -> bool:
        return symbol in self.symbols or enable

    def symbol_info(self, symbol: str) -> Optional[FakeSymbolInfo]:
        return self.symbols.get(symbol)

    def symbol_info_tick(self, symbol: str) -> Optional[FakeTick]:
        return self.ticks.get(symbol)

    def order_check(self, request: dict[str, Any]) -> Any:
        if self.order_check_raises is not None:
            raise self.order_check_raises
        if self.check_retcode != int(Mt5Retcode.DONE):
            return FakeCheckResult(retcode=self.check_retcode)
        return FakeCheckResult(retcode=int(Mt5Retcode.DONE))

    def order_send(self, request: dict[str, Any]) -> Any:
        if self.on_order_send is not None:
            return self.on_order_send(request)
        if self.send_returns_none:
            self.last_error_code = -1
            self.last_error_desc = "timeout"
            return None
        if self.order_send_response is not None:
            return self.order_send_response
        if self.send_retcode != int(Mt5Retcode.DONE):
            return FakeSendResult(
                retcode=self.send_retcode,
                order=0,
                deal=0,
            )
        result = self.send_result or FakeSendResult(
            retcode=int(Mt5Retcode.DONE),
            order=9001,
            deal=0,
        )
        if result.deal == 0:
            deal_ticket = 7000 + len(self.deals) + 1
            self.deals.append(
                FakeDeal(
                    ticket=deal_ticket,
                    magic=int(request["magic"]),
                    comment=str(request["comment"]),
                    volume=float(request["volume"]),
                    price=float(request["price"]),
                    time=self._now_ts(),
                )
            )
            result = FakeSendResult(
                retcode=result.retcode,
                order=result.order,
                deal=deal_ticket,
            )
        self.orders.append(dict(request))
        return result

    def orders_get(self, *, ticket: Optional[int] = None) -> list[dict[str, Any]]:
        if ticket is None:
            return list(self.orders)
        return [o for o in self.orders if int(o.get("ticket", 0)) == ticket]

    def positions_get(self, *, symbol: Optional[str] = None) -> list[Any]:
        return []

    def history_orders_get(self, start: datetime, end: datetime, *, group: str = "") -> list[Any]:
        return []

    def history_deals_get(self, start: datetime, end: datetime, *, group: str = "") -> list[FakeDeal]:
        return [
            deal
            for deal in self.deals
            if start.timestamp() <= deal.time <= end.timestamp()
        ]


def seed_symbol(
    runtime: FakeMt5Runtime,
    symbol: str,
    *,
    filling_mode: int = int(Mt5SymbolFilling.IOC),
    volume_step: float = 1.0,
    tick_time: int | None = None,
) -> None:
    runtime.symbols[symbol] = FakeSymbolInfo(
        filling_mode=filling_mode,
        volume_step=volume_step,
        volume_min=volume_step,
    )
    runtime.ticks[symbol] = FakeTick(time=tick_time or int(datetime.now().timestamp()))


def open_gates(*, account: int = 12345678) -> "LiveExecutionGates":
    from q_backend.execution.brokers.live_gates import LiveExecutionGates

    return LiveExecutionGates(
        enabled=True,
        account_allowlist=frozenset({account}),
        deployment_live_activation_enabled=True,
        controlled_account_validated=True,
    )


def market_request(
    *,
    order_id: UUID,
    symbol: str = "WIN$",
    side: str = "buy",
    quantity: str = "1",
) -> "MarketOrderRequest":
    from decimal import Decimal

    from q_backend.execution.brokers.base import MarketOrderRequest
    from q_backend.execution.domain import ExecutionSide

    return MarketOrderRequest(
        deployment_id=UUID("11111111-1111-1111-1111-111111111111"),
        order_id=order_id,
        symbol=symbol,
        side=ExecutionSide.BUY if side == "buy" else ExecutionSide.SELL,
        quantity=Decimal(quantity),
        external_fill_id=f"mt5:{order_id}",
    )
