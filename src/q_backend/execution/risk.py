"""Pre-trade risk gate for forward execution."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional
from uuid import UUID

from q_backend.execution.brokers.base import ExecutableQuote, PaperCostConfig
from q_backend.execution.brokers.paper import validate_quantity, validate_quote
from q_backend.execution.domain import (
    DeploymentLifecycle,
    ExecutionOrderStatus,
    ExecutionSide,
    PositionSide,
    RiskRejection,
    RiskRejectionCode,
    is_deployment_evaluating,
)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class RiskContext:
    deployment_id: UUID
    paper_account_id: UUID
    lifecycle: DeploymentLifecycle
    symbol: str
    bar_close_time: datetime
    side: ExecutionSide
    quantity: Decimal
    quote: Optional[ExecutableQuote]
    now: datetime
    kill_switch_enabled: bool
    lease_held: bool
    has_unknown_orders: bool
    position_side: PositionSide
    position_quantity: Decimal
    cash_balance: Decimal
    equity: Decimal
    daily_realized_pnl: Decimal
    max_daily_loss: Optional[Decimal]
    max_notional: Optional[Decimal]
    point_value: Decimal
    cost_config: PaperCostConfig
    max_bar_age_seconds: float
    allow_lifecycle_bypass: bool = False


class RiskGate:
    """Structured pre-trade checks; records nothing itself."""

    def evaluate(self, ctx: RiskContext) -> Optional[RiskRejection]:
        checks = (
            self._check_kill_switch,
            self._check_lifecycle,
            self._check_lease,
            self._check_unknown_orders,
            self._check_bar_freshness,
            self._check_symbol_quote,
            self._check_quantity,
            self._check_notional,
            self._check_one_position,
            self._check_equity,
            self._check_daily_loss,
        )
        for check in checks:
            rejection = check(ctx)
            if rejection is not None:
                return rejection
        return None

    def _check_kill_switch(self, ctx: RiskContext) -> Optional[RiskRejection]:
        if ctx.kill_switch_enabled and not ctx.allow_lifecycle_bypass:
            return RiskRejection(
                code=RiskRejectionCode.KILL_SWITCH,
                message="global kill switch is enabled",
            )
        return None

    def _check_lifecycle(self, ctx: RiskContext) -> Optional[RiskRejection]:
        if ctx.allow_lifecycle_bypass:
            return None
        if not is_deployment_evaluating(ctx.lifecycle):
            return RiskRejection(
                code=RiskRejectionCode.LIFECYCLE,
                message=f"deployment lifecycle {ctx.lifecycle.value} blocks new orders",
            )
        return None

    def _check_lease(self, ctx: RiskContext) -> Optional[RiskRejection]:
        if not ctx.lease_held:
            return RiskRejection(
                code=RiskRejectionCode.LEASE_LOST,
                message="worker does not hold the deployment lease",
            )
        return None

    def _check_unknown_orders(self, ctx: RiskContext) -> Optional[RiskRejection]:
        if ctx.has_unknown_orders:
            return RiskRejection(
                code=RiskRejectionCode.UNKNOWN_PRIOR_ORDER,
                message="deployment has an unknown order awaiting reconciliation",
            )
        return None

    def _check_bar_freshness(self, ctx: RiskContext) -> Optional[RiskRejection]:
        age = (_as_utc(ctx.now) - _as_utc(ctx.bar_close_time)).total_seconds()
        if age > ctx.max_bar_age_seconds:
            return RiskRejection(
                code=RiskRejectionCode.STALE_BAR,
                message="bar close is stale",
                context={"age_seconds": age, "max_age_seconds": ctx.max_bar_age_seconds},
            )
        return None

    def _check_symbol_quote(self, ctx: RiskContext) -> Optional[RiskRejection]:
        if ctx.quote is None:
            return RiskRejection(
                code=RiskRejectionCode.SYMBOL_UNAVAILABLE,
                message=f"symbol {ctx.symbol} quote unavailable",
            )
        broker_rejection = validate_quote(
            ctx.quote,
            now=ctx.now,
            max_age_seconds=ctx.cost_config.max_quote_age_seconds,
        )
        if broker_rejection is not None:
            code = RiskRejectionCode.STALE_QUOTE
            if broker_rejection.code.value == "invalid_quote":
                code = RiskRejectionCode.SYMBOL_UNAVAILABLE
            return RiskRejection(
                code=code,
                message=broker_rejection.message,
                context=broker_rejection.context,
            )
        return None

    def _check_quantity(self, ctx: RiskContext) -> Optional[RiskRejection]:
        broker_rejection = validate_quantity(
            ctx.quantity,
            volume_step=ctx.cost_config.volume_step,
            min_volume=ctx.cost_config.min_volume,
        )
        if broker_rejection is not None:
            return RiskRejection(
                code=RiskRejectionCode.INVALID_QUANTITY,
                message=broker_rejection.message,
                context=broker_rejection.context,
            )
        return None

    def _check_notional(self, ctx: RiskContext) -> Optional[RiskRejection]:
        if ctx.max_notional is None or ctx.quote is None:
            return None
        price = ctx.quote.ask if ctx.side == ExecutionSide.BUY else ctx.quote.bid
        notional = price * ctx.quantity * ctx.point_value
        if notional > ctx.max_notional:
            return RiskRejection(
                code=RiskRejectionCode.NOTIONAL_LIMIT,
                message="order notional exceeds configured limit",
                context={
                    "notional": str(notional),
                    "max_notional": str(ctx.max_notional),
                },
            )
        return None

    def _check_one_position(self, ctx: RiskContext) -> Optional[RiskRejection]:
        if ctx.position_side == PositionSide.FLAT:
            return None
        reducing = (ctx.position_side == PositionSide.LONG and ctx.side == ExecutionSide.SELL) or (
            ctx.position_side == PositionSide.SHORT and ctx.side == ExecutionSide.BUY
        )
        same_side_add = (ctx.position_side == PositionSide.LONG and ctx.side == ExecutionSide.BUY) or (
            ctx.position_side == PositionSide.SHORT and ctx.side == ExecutionSide.SELL
        )
        if reducing or same_side_add:
            return None
        return RiskRejection(
            code=RiskRejectionCode.ONE_POSITION_VIOLATION,
            message="order would violate the one-position invariant",
            context={
                "position_side": ctx.position_side.value,
                "order_side": ctx.side.value,
            },
        )

    def _check_equity(self, ctx: RiskContext) -> Optional[RiskRejection]:
        if ctx.equity <= 0:
            return RiskRejection(
                code=RiskRejectionCode.INSUFFICIENT_EQUITY,
                message="account equity is non-positive",
                context={"equity": str(ctx.equity)},
            )
        return None

    def _check_daily_loss(self, ctx: RiskContext) -> Optional[RiskRejection]:
        if ctx.max_daily_loss is None:
            return None
        if ctx.daily_realized_pnl <= -ctx.max_daily_loss:
            return RiskRejection(
                code=RiskRejectionCode.DAILY_LOSS_LIMIT,
                message="maximum daily realized loss reached",
                context={
                    "daily_realized_pnl": str(ctx.daily_realized_pnl),
                    "max_daily_loss": str(ctx.max_daily_loss),
                },
            )
        return None


def deployment_has_unknown_orders(orders: list[Any]) -> bool:
    return any(order.status == ExecutionOrderStatus.UNKNOWN.value for order in orders)
