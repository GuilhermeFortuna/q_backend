"""Durable paper ledger: cash, realized P&L, fees, and single net position."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from q_backend.execution.brokers.base import (
    BrokerAccountSnapshot,
    BrokerPositionSnapshot,
    ExecutableQuote,
)
from q_backend.execution.domain import (
    ExecutionOrderStatus,
    ExecutionSide,
    FillRecord,
    LedgerEntryType,
    PositionSide,
)
from q_backend.storage.db.execution_models import ExecutionLedgerEntry
from q_backend.storage.db.execution_repositories import (
    append_ledger_entry,
    create_execution_fill,
    get_execution_fill_by_external_id,
    get_open_net_position,
    get_paper_account,
    transition_execution_order,
    update_paper_cash_balance,
    upsert_open_net_position,
)


@dataclass(frozen=True)
class LedgerApplyResult:
    fill_id: uuid.UUID
    cash_balance: Decimal
    realized_pnl: Decimal
    fee: Decimal
    position_side: PositionSide
    position_quantity: Decimal
    average_entry_price: Optional[Decimal]
    idempotent: bool = False


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def mark_price_for_position(side: PositionSide, quote: ExecutableQuote) -> Decimal:
    if side == PositionSide.LONG:
        return quote.bid
    if side == PositionSide.SHORT:
        return quote.ask
    return (quote.bid + quote.ask) / Decimal("2")


def unrealized_pnl(
    *,
    side: PositionSide,
    quantity: Decimal,
    average_entry_price: Decimal,
    mark_price: Decimal,
    point_value: Decimal,
) -> Decimal:
    if side == PositionSide.LONG:
        return (mark_price - average_entry_price) * quantity * point_value
    if side == PositionSide.SHORT:
        return (average_entry_price - mark_price) * quantity * point_value
    return Decimal("0")


def realized_pnl_for_close(
    *,
    closing_side: PositionSide,
    quantity: Decimal,
    entry_price: Decimal,
    exit_price: Decimal,
    point_value: Decimal,
) -> Decimal:
    if closing_side == PositionSide.LONG:
        return (exit_price - entry_price) * quantity * point_value
    if closing_side == PositionSide.SHORT:
        return (entry_price - exit_price) * quantity * point_value
    return Decimal("0")


@dataclass(frozen=True)
class _PositionTransition:
    new_side: PositionSide
    new_quantity: Decimal
    new_average_entry_price: Optional[Decimal]
    close_quantity: Decimal
    closing_side: Optional[PositionSide]
    open_quantity: Decimal
    opening_side: Optional[PositionSide]


def _transition_position(
    current_side: PositionSide,
    current_quantity: Decimal,
    current_entry: Optional[Decimal],
    order_side: ExecutionSide,
    order_quantity: Decimal,
    fill_price: Decimal,
) -> _PositionTransition:
    if current_side == PositionSide.FLAT or current_quantity == 0:
        opening_side = (
            PositionSide.LONG if order_side == ExecutionSide.BUY else PositionSide.SHORT
        )
        return _PositionTransition(
            new_side=opening_side,
            new_quantity=order_quantity,
            new_average_entry_price=fill_price,
            close_quantity=Decimal("0"),
            closing_side=None,
            open_quantity=order_quantity,
            opening_side=opening_side,
        )

    is_reducing = (
        current_side == PositionSide.LONG and order_side == ExecutionSide.SELL
    ) or (current_side == PositionSide.SHORT and order_side == ExecutionSide.BUY)

    is_same_side_add = (
        current_side == PositionSide.LONG and order_side == ExecutionSide.BUY
    ) or (current_side == PositionSide.SHORT and order_side == ExecutionSide.SELL)

    if is_same_side_add:
        new_qty = current_quantity + order_quantity
        entry = current_entry or fill_price
        weighted_entry = (
            (entry * current_quantity) + (fill_price * order_quantity)
        ) / new_qty
        return _PositionTransition(
            new_side=current_side,
            new_quantity=new_qty,
            new_average_entry_price=weighted_entry,
            close_quantity=Decimal("0"),
            closing_side=None,
            open_quantity=order_quantity,
            opening_side=current_side,
        )

    if not is_reducing:
        raise ValueError(
            "one-position invariant: order side opposes current position"
        )

    close_qty = min(current_quantity, order_quantity)
    remaining_order = order_quantity - close_qty
    entry = current_entry or fill_price

    if remaining_order == 0:
        new_qty = current_quantity - close_qty
        if new_qty == 0:
            return _PositionTransition(
                new_side=PositionSide.FLAT,
                new_quantity=Decimal("0"),
                new_average_entry_price=None,
                close_quantity=close_qty,
                closing_side=current_side,
                open_quantity=Decimal("0"),
                opening_side=None,
            )
        return _PositionTransition(
            new_side=current_side,
            new_quantity=new_qty,
            new_average_entry_price=entry,
            close_quantity=close_qty,
            closing_side=current_side,
            open_quantity=Decimal("0"),
            opening_side=None,
        )

    opening_side = (
        PositionSide.LONG if order_side == ExecutionSide.BUY else PositionSide.SHORT
    )
    return _PositionTransition(
        new_side=opening_side,
        new_quantity=remaining_order,
        new_average_entry_price=fill_price,
        close_quantity=close_qty,
        closing_side=current_side,
        open_quantity=remaining_order,
        opening_side=opening_side,
    )


def _ledger_totals(session: Session, paper_account_id: uuid.UUID) -> tuple[Decimal, Decimal]:
    realized = session.execute(
        select(func.coalesce(func.sum(ExecutionLedgerEntry.amount), 0)).where(
            ExecutionLedgerEntry.paper_account_id == paper_account_id,
            ExecutionLedgerEntry.entry_type == LedgerEntryType.REALIZED_PNL.value,
        )
    ).scalar_one()
    fees = session.execute(
        select(func.coalesce(func.sum(ExecutionLedgerEntry.amount), 0)).where(
            ExecutionLedgerEntry.paper_account_id == paper_account_id,
            ExecutionLedgerEntry.entry_type == LedgerEntryType.FEE.value,
        )
    ).scalar_one()
    return Decimal(str(realized)), abs(Decimal(str(fees)))


class ExecutionLedger:
    """Apply broker fills to durable account state inside a caller-owned session."""

    def apply_fill(
        self,
        session: Session,
        *,
        paper_account_id: uuid.UUID,
        deployment_id: uuid.UUID,
        order_id: uuid.UUID,
        fill: FillRecord,
        point_value: Decimal,
        symbol: str,
        reconciling: bool = False,
    ) -> LedgerApplyResult:
        existing = get_execution_fill_by_external_id(
            session,
            broker_mode=fill.broker_mode.value,
            external_fill_id=fill.external_fill_id,
        )
        if existing is not None:
            account = get_paper_account(session, paper_account_id)
            if account is None:
                raise ValueError(f"PaperAccount {paper_account_id} not found")
            position = get_open_net_position(session, deployment_id)
            return LedgerApplyResult(
                fill_id=existing.id,
                cash_balance=account.cash_balance,
                realized_pnl=Decimal("0"),
                fee=existing.fee,
                position_side=PositionSide(position.side) if position else PositionSide.FLAT,
                position_quantity=position.quantity if position else Decimal("0"),
                average_entry_price=position.average_entry_price if position else None,
                idempotent=True,
            )

        account = get_paper_account(session, paper_account_id)
        if account is None:
            raise ValueError(f"PaperAccount {paper_account_id} not found")

        open_position = get_open_net_position(session, deployment_id)
        current_side = (
            PositionSide(open_position.side)
            if open_position and open_position.is_open
            else PositionSide.FLAT
        )
        current_qty = open_position.quantity if open_position and open_position.is_open else Decimal("0")
        current_entry = open_position.average_entry_price if open_position else None

        transition = _transition_position(
            current_side,
            current_qty,
            current_entry,
            fill.side,
            fill.quantity,
            fill.price,
        )

        realized = Decimal("0")
        if transition.close_quantity > 0 and transition.closing_side is not None:
            realized = realized_pnl_for_close(
                closing_side=transition.closing_side,
                quantity=transition.close_quantity,
                entry_price=current_entry or fill.price,
                exit_price=fill.price,
                point_value=point_value,
            )

        cash = account.cash_balance + realized - fill.fee

        persisted_fill = create_execution_fill(
            session,
            deployment_id=deployment_id,
            order_id=order_id,
            broker_mode=fill.broker_mode,
            external_fill_id=fill.external_fill_id,
            side=fill.side,
            quantity=fill.quantity,
            price=fill.price,
            filled_at=fill.filled_at,
            fee=fill.fee,
            slippage=fill.slippage,
            quote_bid=fill.quote_bid,
            quote_ask=fill.quote_ask,
            quote_timestamp=fill.quote_timestamp,
            metadata=fill.metadata,
        )

        if realized != 0:
            append_ledger_entry(
                session,
                paper_account_id=paper_account_id,
                deployment_id=deployment_id,
                entry_type=LedgerEntryType.REALIZED_PNL,
                amount=realized,
                balance_after=cash,
                fill_id=persisted_fill.id,
                description=f"{symbol} close {transition.close_quantity}",
            )
        if fill.fee != 0:
            append_ledger_entry(
                session,
                paper_account_id=paper_account_id,
                deployment_id=deployment_id,
                entry_type=LedgerEntryType.FEE,
                amount=-fill.fee,
                balance_after=cash,
                fill_id=persisted_fill.id,
                description=f"{symbol} commission",
            )

        update_paper_cash_balance(session, paper_account_id, cash)
        upsert_open_net_position(
            session,
            deployment_id=deployment_id,
            side=transition.new_side,
            quantity=transition.new_quantity,
            average_entry_price=transition.new_average_entry_price,
            opened_at=fill.filled_at,
        )
        if reconciling:
            # Reconciled orders are already UNKNOWN; go straight to FILLED
            # (UNKNOWN -> SUBMITTED is not a legal transition).
            transition_execution_order(
                session,
                order_id,
                ExecutionOrderStatus.FILLED,
                submitted_at=fill.filled_at,
                completed_at=fill.filled_at,
            )
        else:
            transition_execution_order(
                session,
                order_id,
                ExecutionOrderStatus.SUBMITTED,
                submitted_at=fill.filled_at,
            )
            transition_execution_order(
                session,
                order_id,
                ExecutionOrderStatus.FILLED,
                completed_at=fill.filled_at,
            )

        return LedgerApplyResult(
            fill_id=persisted_fill.id,
            cash_balance=cash,
            realized_pnl=realized,
            fee=fill.fee,
            position_side=transition.new_side,
            position_quantity=transition.new_quantity,
            average_entry_price=transition.new_average_entry_price,
        )

    def account_snapshot(
        self,
        session: Session,
        *,
        paper_account_id: uuid.UUID,
        deployment_id: uuid.UUID,
        symbol: str,
        quote: Optional[ExecutableQuote],
        point_value: Decimal,
        as_of: Optional[datetime] = None,
    ) -> BrokerAccountSnapshot:
        account = get_paper_account(session, paper_account_id)
        if account is None:
            raise ValueError(f"PaperAccount {paper_account_id} not found")

        position = get_open_net_position(session, deployment_id)
        positions: list[BrokerPositionSnapshot] = []
        total_unrealized = Decimal("0")

        if position and position.is_open and position.quantity > 0:
            side = PositionSide(position.side)
            mark = None
            unrealized = Decimal("0")
            if quote is not None:
                mark = mark_price_for_position(side, quote)
                unrealized = unrealized_pnl(
                    side=side,
                    quantity=position.quantity,
                    average_entry_price=position.average_entry_price or Decimal("0"),
                    mark_price=mark,
                    point_value=point_value,
                )
            total_unrealized += unrealized
            positions.append(
                BrokerPositionSnapshot(
                    deployment_id=deployment_id,
                    symbol=symbol,
                    side=side,
                    quantity=position.quantity,
                    average_entry_price=position.average_entry_price,
                    unrealized_pnl=unrealized,
                    mark_price=mark,
                )
            )

        realized_total, fee_total = _ledger_totals(session, paper_account_id)
        equity = account.cash_balance + total_unrealized

        return BrokerAccountSnapshot(
            account_id=paper_account_id,
            currency=account.currency,
            cash_balance=account.cash_balance,
            equity=equity,
            unrealized_pnl=total_unrealized,
            realized_pnl=realized_total,
            total_fees=fee_total,
            positions=positions,
            as_of=as_of or _as_utc(datetime.now(timezone.utc)),
        )
