"""Internal paper broker: live quote shapes, deterministic fills, no MT5 orders."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from q_backend.execution.brokers.base import (
    BrokerHealth,
    BrokerOrderLookupStatus,
    BrokerOrderState,
    BrokerRejection,
    BrokerRejectionCode,
    BrokerSubmissionOutcome,
    BrokerSubmissionResult,
    Clock,
    ExecutableQuote,
    MarketOrderRequest,
    PaperCostConfig,
    QuoteSource,
)
from q_backend.execution.domain import BrokerMode, ExecutionSide, FillRecord


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def validate_quote(
    quote: Optional[ExecutableQuote],
    *,
    now: datetime,
    max_age_seconds: float,
) -> Optional[BrokerRejection]:
    if quote is None:
        return BrokerRejection(
            code=BrokerRejectionCode.QUOTE_UNAVAILABLE,
            message="executable quote is unavailable",
        )
    if quote.bid <= 0 or quote.ask <= 0:
        return BrokerRejection(
            code=BrokerRejectionCode.INVALID_QUOTE,
            message="quote prices must be positive",
            context={"bid": str(quote.bid), "ask": str(quote.ask)},
        )
    if quote.bid >= quote.ask:
        return BrokerRejection(
            code=BrokerRejectionCode.INVALID_QUOTE,
            message="quote is crossed or zero spread",
            context={"bid": str(quote.bid), "ask": str(quote.ask)},
        )
    age = (_as_utc(now) - _as_utc(quote.timestamp)).total_seconds()
    if age > max_age_seconds:
        return BrokerRejection(
            code=BrokerRejectionCode.STALE_QUOTE,
            message="quote is stale",
            context={"age_seconds": age, "max_age_seconds": max_age_seconds},
        )
    return None


def validate_quantity(
    quantity: Decimal,
    *,
    volume_step: Decimal,
    min_volume: Decimal,
) -> Optional[BrokerRejection]:
    if quantity <= 0:
        return BrokerRejection(
            code=BrokerRejectionCode.INVALID_QUANTITY,
            message="quantity must be positive",
            context={"quantity": str(quantity)},
        )
    if quantity < min_volume:
        return BrokerRejection(
            code=BrokerRejectionCode.INVALID_QUANTITY,
            message="quantity is below minimum volume",
            context={"quantity": str(quantity), "min_volume": str(min_volume)},
        )
    remainder = quantity % volume_step
    if remainder != 0:
        return BrokerRejection(
            code=BrokerRejectionCode.INVALID_QUANTITY,
            message="quantity is not aligned to volume step",
            context={
                "quantity": str(quantity),
                "volume_step": str(volume_step),
            },
        )
    return None


def executable_fill_price(
    side: ExecutionSide,
    quote: ExecutableQuote,
    slippage_points: Decimal,
) -> Decimal:
    if side == ExecutionSide.BUY:
        return quote.ask + slippage_points
    return quote.bid - slippage_points


def compute_commission(
    cost: PaperCostConfig,
    *,
    price: Decimal,
    quantity: Decimal,
) -> Decimal:
    per_contract = cost.cost_per_contract * quantity
    notional = price * quantity * cost.point_value
    bps_part = (cost.cost_bps / Decimal("10000")) * notional
    return per_contract + bps_part


class PaperBroker:
    """Deterministic internal market-order simulator backed by executable quotes."""

    def __init__(
        self,
        *,
        quote_source: QuoteSource,
        clock: Clock,
        broker_mode: BrokerMode = BrokerMode.PAPER,
    ) -> None:
        self._quote_source = quote_source
        self._clock = clock
        self._broker_mode = broker_mode

    def health(self) -> BrokerHealth:
        now = self._clock.now()
        return BrokerHealth(
            broker_mode=self._broker_mode,
            is_available=True,
            message="paper broker ready",
            checked_at=now,
        )

    def submit_market_order(
        self,
        request: MarketOrderRequest,
        *,
        cost_config: PaperCostConfig,
    ) -> BrokerSubmissionResult:
        now = self._clock.now()
        quote = self._quote_source.get_quote(request.symbol)
        quote_rejection = validate_quote(
            quote,
            now=now,
            max_age_seconds=cost_config.max_quote_age_seconds,
        )
        if quote_rejection is not None:
            return BrokerSubmissionResult(
                outcome=BrokerSubmissionOutcome.REJECTED,
                rejection=quote_rejection,
                cost_config=cost_config,
            )

        quantity_rejection = validate_quantity(
            request.quantity,
            volume_step=cost_config.volume_step,
            min_volume=cost_config.min_volume,
        )
        if quantity_rejection is not None:
            return BrokerSubmissionResult(
                outcome=BrokerSubmissionOutcome.REJECTED,
                rejection=quantity_rejection,
                cost_config=cost_config,
            )

        assert quote is not None
        fill_price = executable_fill_price(request.side, quote, cost_config.slippage_points)
        fee = compute_commission(cost_config, price=fill_price, quantity=request.quantity)
        fill = FillRecord(
            broker_mode=self._broker_mode,
            external_fill_id=request.external_fill_id,
            side=request.side,
            quantity=request.quantity,
            price=fill_price,
            fee=fee,
            slippage=cost_config.slippage_points,
            quote_bid=quote.bid,
            quote_ask=quote.ask,
            quote_timestamp=quote.timestamp,
            filled_at=now,
            metadata={
                "symbol": request.symbol,
                "deployment_id": str(request.deployment_id),
                "order_id": str(request.order_id),
                "cost_config": cost_config.model_dump(mode="json"),
            },
        )
        return BrokerSubmissionResult(
            outcome=BrokerSubmissionOutcome.FILLED,
            fill=fill,
            cost_config=cost_config,
        )

    def lookup_order(self, request: MarketOrderRequest) -> BrokerOrderState:
        """The paper broker holds no external order state of its own.

        Paper fills exist only as durable rows in Q's own ledger; the simulator
        never records anything outside that transaction. So if an order is still
        unknown, the fill provably never happened, and the authoritative answer
        is ``NOT_FOUND`` — the reconciler fails the order and the strategy may
        re-decide on a later bar. It never re-derives a fill from a newer quote.
        """
        return BrokerOrderState(
            status=BrokerOrderLookupStatus.NOT_FOUND,
            message=("paper broker keeps no external order state; " "Q ledger is authoritative"),
        )
