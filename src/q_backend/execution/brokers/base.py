"""Broker-neutral contracts for forward execution adapters."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Optional, Protocol, runtime_checkable
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from q_backend.execution.domain import (
    BrokerMode,
    ExecutionOrderType,
    ExecutionSide,
    FillRecord,
    PositionSide,
)


class BrokerRejectionCode(str, Enum):
    QUOTE_UNAVAILABLE = "quote_unavailable"
    STALE_QUOTE = "stale_quote"
    INVALID_QUOTE = "invalid_quote"
    INVALID_QUANTITY = "invalid_quantity"
    SYMBOL_UNAVAILABLE = "symbol_unavailable"
    BROKER_UNAVAILABLE = "broker_unavailable"
    LIVE_LOCKED = "live_locked"
    ACCOUNT_NOT_ALLOWLISTED = "account_not_allowlisted"
    TRADING_DISABLED = "trading_disabled"
    UNSUPPORTED_FILL_POLICY = "unsupported_fill_policy"
    UNSUPPORTED_ORDER_KIND = "unsupported_order_kind"
    ORDER_CHECK_FAILED = "order_check_failed"
    ORDER_SEND_FAILED = "order_send_failed"
    PROTECTIVE_LEVEL_REJECTED = "protective_level_rejected"


class BrokerSubmissionOutcome(str, Enum):
    FILLED = "filled"
    WORKING = "working"
    APPLIED = "applied"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    UNKNOWN = "unknown"


class BrokerOrderLookupStatus(str, Enum):
    """Result of asking a broker about the fate of a previously-intended order."""

    FILLED = "filled"
    WORKING = "working"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    REJECTED = "rejected"
    NOT_FOUND = "not_found"
    UNAVAILABLE = "unavailable"


class PaperCostConfig(BaseModel):
    """Deterministic paper fill cost model (no MT5 constants)."""

    model_config = ConfigDict(frozen=True)

    point_value: Decimal = Field(default=Decimal("1"), gt=0)
    slippage_points: Decimal = Field(default=Decimal("0"), ge=0)
    cost_per_contract: Decimal = Field(default=Decimal("0"), ge=0)
    cost_bps: Decimal = Field(default=Decimal("0"), ge=0)
    max_quote_age_seconds: float = Field(default=30.0, gt=0)
    volume_step: Decimal = Field(default=Decimal("1"), gt=0)
    min_volume: Decimal = Field(default=Decimal("1"), gt=0)


@runtime_checkable
class Clock(Protocol):
    def now(self) -> datetime: ...


@runtime_checkable
class QuoteSource(Protocol):
    def get_quote(self, symbol: str) -> Optional["ExecutableQuote"]: ...


class ExecutableQuote(BaseModel):
    model_config = ConfigDict(frozen=True)

    symbol: str
    bid: Decimal
    ask: Decimal
    timestamp: datetime
    source: str = "quote_source"


class BrokerHealth(BaseModel):
    model_config = ConfigDict(frozen=True)

    broker_mode: BrokerMode
    is_available: bool
    message: str
    checked_at: datetime


class BrokerPositionSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True)

    deployment_id: UUID
    symbol: str
    side: PositionSide
    quantity: Decimal
    average_entry_price: Optional[Decimal] = None
    unrealized_pnl: Decimal = Decimal("0")
    mark_price: Optional[Decimal] = None


class BrokerAccountSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True)

    account_id: UUID
    currency: str
    cash_balance: Decimal
    equity: Decimal
    unrealized_pnl: Decimal
    realized_pnl: Decimal
    total_fees: Decimal
    positions: list[BrokerPositionSnapshot] = Field(default_factory=list)
    as_of: datetime


class MarketOrderRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    deployment_id: UUID
    order_id: UUID
    symbol: str
    side: ExecutionSide
    quantity: Decimal
    external_fill_id: str
    broker_mode: BrokerMode = BrokerMode.PAPER
    intent_created_at: datetime
    live_activation_enabled: bool = False
    stop_loss: Optional[Decimal] = None
    take_profit: Optional[Decimal] = None


class PendingOrderRequest(BaseModel):
    """A stop or limit entry resting at the broker until it fills, expires or is removed."""

    model_config = ConfigDict(frozen=True)

    deployment_id: UUID
    order_id: UUID
    symbol: str
    side: ExecutionSide
    order_type: ExecutionOrderType
    quantity: Decimal
    price: Decimal
    intent_created_at: datetime
    broker_mode: BrokerMode = BrokerMode.PAPER
    stop_loss: Optional[Decimal] = None
    take_profit: Optional[Decimal] = None
    expires_at: Optional[datetime] = None
    live_activation_enabled: bool = False


class ProtectionRequest(BaseModel):
    """New stop-loss and take-profit levels for an open broker position."""

    model_config = ConfigDict(frozen=True)

    deployment_id: UUID
    order_id: UUID
    symbol: str
    position_ticket: str
    stop_loss: Optional[Decimal]
    take_profit: Optional[Decimal]
    intent_created_at: datetime
    broker_mode: BrokerMode = BrokerMode.PAPER
    live_activation_enabled: bool = False


class PositionExitRequest(BaseModel):
    """Question to the broker: did this position close on the broker side (stop, target or stop-out)?"""

    model_config = ConfigDict(frozen=True)

    deployment_id: UUID
    symbol: str
    position_ticket: str
    opened_at: datetime
    broker_mode: BrokerMode = BrokerMode.PAPER


class CancelOrderRequest(BaseModel):
    """Removal of a working pending order at the broker."""

    model_config = ConfigDict(frozen=True)

    deployment_id: UUID
    order_id: UUID
    symbol: str
    external_order_id: str
    intent_created_at: datetime
    broker_mode: BrokerMode = BrokerMode.PAPER
    live_activation_enabled: bool = False


class BrokerRejection(BaseModel):
    model_config = ConfigDict(frozen=True)

    code: BrokerRejectionCode
    message: str
    context: dict[str, Any] = Field(default_factory=dict)


class BrokerSubmissionResult(BaseModel):
    outcome: BrokerSubmissionOutcome = BrokerSubmissionOutcome.REJECTED
    fill: Optional[FillRecord] = None
    rejection: Optional[BrokerRejection] = None
    cost_config: Optional[PaperCostConfig] = None
    external_order_id: Optional[str] = None
    external_position_id: Optional[str] = None
    external_deal_ids: tuple[str, ...] = ()
    raw_request: dict[str, Any] = Field(default_factory=dict)
    raw_response: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def accepted(self) -> bool:
        return self.outcome == BrokerSubmissionOutcome.FILLED


class BrokerOrderState(BaseModel):
    """Read-only answer to ``lookup_order``; never triggers a (re)submission."""

    model_config = ConfigDict(frozen=True)

    status: BrokerOrderLookupStatus
    fill: Optional[FillRecord] = None
    external_order_id: Optional[str] = None
    external_position_id: Optional[str] = None
    external_deal_ids: tuple[str, ...] = ()
    message: str = ""
    fills: tuple[FillRecord, ...] = ()


@runtime_checkable
class ExecutionBroker(Protocol):
    def health(self) -> BrokerHealth: ...

    def submit_market_order(
        self,
        request: MarketOrderRequest,
        *,
        cost_config: PaperCostConfig,
    ) -> BrokerSubmissionResult: ...

    def lookup_order(self, request: MarketOrderRequest) -> BrokerOrderState:
        """Look up an order's outcome by its client order id (read-only)."""
        ...

    def submit_pending_order(self, request: PendingOrderRequest) -> BrokerSubmissionResult: ...

    def lookup_pending_order(self, request: PendingOrderRequest) -> BrokerOrderState: ...

    def set_protection(self, request: ProtectionRequest) -> BrokerSubmissionResult: ...

    def cancel_order(self, request: CancelOrderRequest) -> BrokerSubmissionResult: ...

    def lookup_protection(self, request: ProtectionRequest) -> BrokerOrderState:
        """Read-only: does the position already carry the requested levels?"""
        ...

    def lookup_cancellation(self, request: CancelOrderRequest) -> BrokerOrderState:
        """Read-only: has the working order left the broker's order list?"""
        ...

    def lookup_position_exit(self, request: PositionExitRequest) -> BrokerOrderState:
        """Read-only: the aggregated broker-side exit of a position, with its reason in fill metadata."""
        ...
