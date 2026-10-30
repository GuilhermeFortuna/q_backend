"""Map durable execution positions to backtest ``Trade`` views."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional
from uuid import UUID

from q_backend.backtesting.models import OrderAction, Trade, TradeStatus
from q_backend.execution.domain import PositionSide


def position_trade_id(deployment_id: UUID | str, opened_at: datetime) -> str:
    """Stable per-position trade id derived from durable ``opened_at``."""
    ts = opened_at if opened_at.tzinfo is not None else opened_at.replace(tzinfo=timezone.utc)
    ts = ts.astimezone(timezone.utc)
    epoch_ms = int(ts.timestamp() * 1000)
    return f"exec-{deployment_id}-{epoch_ms}"


def execution_position_to_trade(
    *,
    deployment_id: UUID | str,
    symbol: str,
    side: PositionSide,
    quantity: Decimal,
    average_entry_price: Optional[Decimal],
    opened_at: Optional[datetime],
    point_value: float = 1.0,
    stop_loss: Optional[Decimal] = None,
    take_profit: Optional[Decimal] = None,
) -> Optional[Trade]:
    """Convert an open net position into the minimal ``Trade`` view strategies expect."""
    if side == PositionSide.FLAT or quantity <= 0:
        return None

    if average_entry_price is None or opened_at is None:
        raise ValueError("open execution position requires entry price and opened_at")

    if opened_at.tzinfo is None:
        # Some stores return naive timestamps (SQLite); the ledger writes UTC.
        opened_at = opened_at.replace(tzinfo=timezone.utc)
    action = OrderAction.BUY if side == PositionSide.LONG else OrderAction.SELL
    trade_id = position_trade_id(deployment_id, opened_at)
    return Trade(
        id=trade_id,
        order_id=f"exec-order-{trade_id}",
        symbol=symbol,
        action=action,
        quantity=float(quantity),
        entry_time=opened_at,
        entry_price=float(average_entry_price),
        status=TradeStatus.OPEN,
        point_value=point_value,
        stop_loss=float(stop_loss) if stop_loss is not None else None,
        take_profit=float(take_profit) if take_profit is not None else None,
    )
