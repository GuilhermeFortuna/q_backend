"""Map durable execution positions to backtest ``Trade`` views."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Optional
from uuid import UUID

from q_backend.backtesting.models import OrderAction, Trade, TradeStatus
from q_backend.execution.domain import PositionSide


def execution_position_to_trade(
    *,
    deployment_id: UUID | str,
    symbol: str,
    side: PositionSide,
    quantity: Decimal,
    average_entry_price: Optional[Decimal],
    opened_at: Optional[datetime],
    point_value: float = 1.0,
) -> Optional[Trade]:
    """Convert an open net position into the minimal ``Trade`` view strategies expect."""
    if side == PositionSide.FLAT or quantity <= 0:
        return None

    if average_entry_price is None or opened_at is None:
        raise ValueError("open execution position requires entry price and opened_at")

    action = OrderAction.BUY if side == PositionSide.LONG else OrderAction.SELL
    trade_id = f"exec-{deployment_id}"
    return Trade(
        id=trade_id,
        order_id=f"exec-order-{deployment_id}",
        symbol=symbol,
        action=action,
        quantity=float(quantity),
        entry_time=opened_at,
        entry_price=float(average_entry_price),
        status=TradeStatus.OPEN,
        point_value=point_value,
    )
