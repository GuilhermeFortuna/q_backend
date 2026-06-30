"""Broker-neutral execution domain types and lifecycle rules."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field


class BrokerMode(str, Enum):
    PAPER = "paper"
    MT5_LIVE = "mt5_live"


class DeploymentLifecycle(str, Enum):
    DRAFT = "draft"
    RUNNING = "running"
    PAUSED = "paused"
    STOPPED = "stopped"
    ERROR = "error"


class DecisionOutcome(str, Enum):
    HOLD = "hold"
    SIGNAL = "signal"
    RISK_REJECTED = "risk_rejected"
    ORDER_INTENT = "order_intent"
    ORDER_FILLED = "order_filled"
    ORDER_REJECTED = "order_rejected"
    ORDER_UNKNOWN = "order_unknown"
    ERROR = "error"


class SignalAction(str, Enum):
    BUY = "buy"
    SELL = "sell"
    HOLD = "hold"
    CLOSE = "close"


class ExecutionSide(str, Enum):
    BUY = "buy"
    SELL = "sell"


class ExecutionOrderType(str, Enum):
    MARKET = "market"


class ExecutionOrderStatus(str, Enum):
    INTENT = "intent"
    SUBMITTED = "submitted"
    FILLED = "filled"
    REJECTED = "rejected"
    UNKNOWN = "unknown"
    CANCELLED = "cancelled"


class PositionSide(str, Enum):
    LONG = "long"
    SHORT = "short"
    FLAT = "flat"


class ReconciliationState(str, Enum):
    NOT_APPLICABLE = "not_applicable"
    PENDING = "pending"
    RECONCILED = "reconciled"
    AMBIGUOUS = "ambiguous"


class RiskRejectionCode(str, Enum):
    KILL_SWITCH = "kill_switch"
    LIFECYCLE = "lifecycle"
    LEASE_LOST = "lease_lost"
    STALE_BAR = "stale_bar"
    STALE_QUOTE = "stale_quote"
    SYMBOL_UNAVAILABLE = "symbol_unavailable"
    INVALID_QUANTITY = "invalid_quantity"
    NOTIONAL_LIMIT = "notional_limit"
    ONE_POSITION_VIOLATION = "one_position_violation"
    INSUFFICIENT_EQUITY = "insufficient_equity"
    DAILY_LOSS_LIMIT = "daily_loss_limit"
    DATABASE_UNAVAILABLE = "database_unavailable"
    BROKER_UNAVAILABLE = "broker_unavailable"
    UNKNOWN_PRIOR_ORDER = "unknown_prior_order"


class LedgerEntryType(str, Enum):
    INITIAL_BALANCE = "initial_balance"
    FILL_CASH = "fill_cash"
    REALIZED_PNL = "realized_pnl"
    FEE = "fee"
    ADJUSTMENT = "adjustment"


class IllegalLifecycleTransition(ValueError):
    """Raised when a deployment or order status change is not permitted."""


_DEPLOYMENT_TRANSITIONS: dict[DeploymentLifecycle, frozenset[DeploymentLifecycle]] = {
    DeploymentLifecycle.DRAFT: frozenset(
        {DeploymentLifecycle.RUNNING, DeploymentLifecycle.STOPPED}
    ),
    DeploymentLifecycle.RUNNING: frozenset(
        {
            DeploymentLifecycle.PAUSED,
            DeploymentLifecycle.STOPPED,
            DeploymentLifecycle.ERROR,
        }
    ),
    DeploymentLifecycle.PAUSED: frozenset(
        {
            DeploymentLifecycle.RUNNING,
            DeploymentLifecycle.STOPPED,
            DeploymentLifecycle.ERROR,
        }
    ),
    DeploymentLifecycle.STOPPED: frozenset(),
    DeploymentLifecycle.ERROR: frozenset({DeploymentLifecycle.STOPPED}),
}

_ORDER_TRANSITIONS: dict[ExecutionOrderStatus, frozenset[ExecutionOrderStatus]] = {
    ExecutionOrderStatus.INTENT: frozenset(
        {
            ExecutionOrderStatus.SUBMITTED,
            ExecutionOrderStatus.REJECTED,
            ExecutionOrderStatus.UNKNOWN,
            ExecutionOrderStatus.CANCELLED,
        }
    ),
    ExecutionOrderStatus.SUBMITTED: frozenset(
        {
            ExecutionOrderStatus.FILLED,
            ExecutionOrderStatus.REJECTED,
            ExecutionOrderStatus.UNKNOWN,
        }
    ),
    ExecutionOrderStatus.UNKNOWN: frozenset(
        {
            ExecutionOrderStatus.FILLED,
            ExecutionOrderStatus.REJECTED,
        }
    ),
    ExecutionOrderStatus.FILLED: frozenset(),
    ExecutionOrderStatus.REJECTED: frozenset(),
    ExecutionOrderStatus.CANCELLED: frozenset(),
}


def validate_deployment_transition(
    current: DeploymentLifecycle,
    target: DeploymentLifecycle,
) -> DeploymentLifecycle:
    allowed = _DEPLOYMENT_TRANSITIONS.get(current, frozenset())
    if target not in allowed:
        raise IllegalLifecycleTransition(
            f"deployment lifecycle cannot transition from {current.value} to {target.value}"
        )
    return target


def validate_order_transition(
    current: ExecutionOrderStatus,
    target: ExecutionOrderStatus,
) -> ExecutionOrderStatus:
    allowed = _ORDER_TRANSITIONS.get(current, frozenset())
    if target not in allowed:
        raise IllegalLifecycleTransition(
            f"order status cannot transition from {current.value} to {target.value}"
        )
    return target


def is_deployment_evaluating(lifecycle: DeploymentLifecycle) -> bool:
    return lifecycle == DeploymentLifecycle.RUNNING


def is_deployment_terminal(lifecycle: DeploymentLifecycle) -> bool:
    return lifecycle in {DeploymentLifecycle.STOPPED, DeploymentLifecycle.ERROR}


class StrategyIdentity(BaseModel):
    """Immutable strategy/config identity carried on deployments and decisions."""

    model_config = ConfigDict(frozen=True)

    strategy_name: str
    strategy_version: int
    compiled_config: dict[str, Any]
    config_hash: str
    symbol: str
    timeframe: str
    sizing_config: dict[str, Any] = Field(default_factory=dict)
    risk_config: dict[str, Any] = Field(default_factory=dict)


class FillRecord(BaseModel):
    """Domain fill snapshot (durable row is separate from backtest Trade)."""

    model_config = ConfigDict(frozen=True)

    broker_mode: BrokerMode
    external_fill_id: str
    side: ExecutionSide
    quantity: Decimal
    price: Decimal
    fee: Decimal = Decimal("0")
    slippage: Decimal = Decimal("0")
    quote_bid: Optional[Decimal] = None
    quote_ask: Optional[Decimal] = None
    quote_timestamp: Optional[datetime] = None
    filled_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)


class RiskRejection(BaseModel):
    """Structured risk rejection before order intent is committed."""

    model_config = ConfigDict(frozen=True)

    code: RiskRejectionCode
    message: str
    context: dict[str, Any] = Field(default_factory=dict)
