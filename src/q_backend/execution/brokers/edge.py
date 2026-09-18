"""MT5 live broker adapter that routes through the execution edge (Q-042)."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Optional
from uuid import UUID

from q_contracts.edge import ExecutionOrder
from q_backend.execution.brokers.base import (
    BrokerHealth,
    BrokerOrderLookupStatus,
    BrokerOrderState,
    BrokerRejection,
    BrokerRejectionCode,
    BrokerSubmissionOutcome,
    BrokerSubmissionResult,
    Clock,
    MarketOrderRequest,
    PaperCostConfig,
)
from q_backend.execution.brokers.live_gates import LiveExecutionGates, evaluate_live_gates
from q_backend.execution.brokers.mt5_constants import retcode_label
from q_backend.execution.domain import BrokerMode, ExecutionSide, FillRecord
from q_backend.execution.edge_client import EdgeClient, EdgeUnavailable

logger = logging.getLogger(__name__)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _execution_order(request: MarketOrderRequest, *, deviation: int) -> ExecutionOrder:
    return ExecutionOrder(
        symbol=request.symbol,
        volume=float(request.quantity),
        side="buy" if request.side == ExecutionSide.BUY else "sell",
        deviation=deviation,
    )


def _fill_from_deals(
    request: MarketOrderRequest,
    deals: list[dict[str, Any]],
    *,
    external_order_id: Optional[str],
    now: datetime,
) -> Optional[tuple[FillRecord, tuple[str, ...]]]:
    if not deals:
        return None

    seen: set[str] = set()
    deal_ids: list[str] = []
    total_qty = Decimal("0")
    notional = Decimal("0")
    latest_time = now
    for deal in sorted(deals, key=lambda d: int(d.get("time_msc", 0) or 0)):
        ticket = str(int(deal.get("ticket", 0)))
        if ticket in seen or ticket == "0":
            continue
        seen.add(ticket)
        deal_ids.append(ticket)
        qty = Decimal(str(deal.get("volume", 0)))
        price = Decimal(str(deal.get("price", 0)))
        total_qty += qty
        notional += qty * price
        time_msc = deal.get("time_msc")
        if time_msc is not None:
            latest_time = datetime.fromtimestamp(int(time_msc) / 1000.0, tz=timezone.utc)

    if total_qty <= 0:
        return None

    avg_price = notional / total_qty
    fill = FillRecord(
        broker_mode=BrokerMode.MT5_LIVE,
        external_fill_id=deal_ids[-1],
        side=request.side,
        quantity=total_qty,
        price=avg_price,
        fee=Decimal("0"),
        slippage=Decimal("0"),
        filled_at=latest_time,
        metadata={
            "symbol": request.symbol,
            "deployment_id": str(request.deployment_id),
            "order_id": str(request.order_id),
            "external_order_id": external_order_id,
            "deal_tickets": deal_ids,
        },
    )
    return fill, tuple(deal_ids)


class EdgeBroker:
    """Submit and reconcile live orders exclusively through the execution edge."""

    def __init__(
        self,
        *,
        client: EdgeClient,
        clock: Clock,
        gates: LiveExecutionGates,
        slippage_deviation: int = 20,
        lookup_window_lead_s: float = 60.0,
    ) -> None:
        self._client = client
        self._clock = clock
        self._gates = gates
        self._slippage_deviation = slippage_deviation
        self._lookup_window_lead_s = lookup_window_lead_s

    def health(self) -> BrokerHealth:
        now = self._clock.now()
        try:
            health = self._client.health()
        except EdgeUnavailable as exc:
            return BrokerHealth(
                broker_mode=BrokerMode.MT5_LIVE,
                is_available=False,
                message=str(exc),
                checked_at=now,
            )
        available = health.mt5_connected
        message = "execution edge connected" if available else "execution edge reports MT5 disconnected"
        return BrokerHealth(
            broker_mode=BrokerMode.MT5_LIVE,
            is_available=available,
            message=message,
            checked_at=now,
        )

    def _lookup_window(self, request: MarketOrderRequest) -> tuple[datetime, datetime]:
        now = self._clock.now()
        lead = timedelta(seconds=self._lookup_window_lead_s)
        start = _as_utc(request.intent_created_at) - lead
        end = _as_utc(now) + lead
        return start, end

    def _lookup_state(
        self,
        request: MarketOrderRequest,
        *,
        external_order_id: Optional[str] = None,
    ) -> BrokerOrderState:
        window_start, window_end = self._lookup_window(request)
        try:
            outcome = self._client.lookup(request.order_id, window_start, window_end)
        except EdgeUnavailable as exc:
            return BrokerOrderState(
                status=BrokerOrderLookupStatus.UNAVAILABLE,
                message=str(exc),
            )

        result = str(outcome.get("outcome", ""))
        if result == "filled":
            deals = outcome.get("deals") or []
            reconciled = _fill_from_deals(
                request,
                deals,
                external_order_id=external_order_id,
                now=self._clock.now(),
            )
            if reconciled is None:
                return BrokerOrderState(
                    status=BrokerOrderLookupStatus.UNAVAILABLE,
                    message="edge reported filled without usable deal details",
                )
            fill, deal_ids = reconciled
            return BrokerOrderState(
                status=BrokerOrderLookupStatus.FILLED,
                fill=fill,
                external_order_id=external_order_id,
                external_deal_ids=deal_ids,
                message="reconciled from edge deal history",
            )

        if result == "rejected":
            reason = outcome.get("reason") or retcode_label(int(outcome.get("retcode", 0)))
            return BrokerOrderState(
                status=BrokerOrderLookupStatus.REJECTED,
                message=reason,
            )

        if result == "not_found":
            return BrokerOrderState(
                status=BrokerOrderLookupStatus.NOT_FOUND,
                message="no edge deal matches the intent",
            )

        return BrokerOrderState(
            status=BrokerOrderLookupStatus.UNAVAILABLE,
            message=str(outcome.get("reason", "edge lookup unavailable")),
        )

    def submit_market_order(
        self,
        request: MarketOrderRequest,
        *,
        cost_config: PaperCostConfig,
    ) -> BrokerSubmissionResult:
        gates = LiveExecutionGates(
            enabled=self._gates.enabled,
            account_allowlist=self._gates.account_allowlist,
            deployment_live_activation_enabled=request.live_activation_enabled,
            controlled_account_validated=self._gates.controlled_account_validated,
        )

        try:
            account = self._client.account()
        except EdgeUnavailable as exc:
            return BrokerSubmissionResult(
                outcome=BrokerSubmissionOutcome.REJECTED,
                rejection=BrokerRejection(
                    code=BrokerRejectionCode.BROKER_UNAVAILABLE,
                    message=str(exc),
                ),
                cost_config=cost_config,
            )

        gate_rejection = evaluate_live_gates(gates, account_login=account.login)
        if gate_rejection is not None:
            return BrokerSubmissionResult(
                outcome=BrokerSubmissionOutcome.REJECTED,
                rejection=gate_rejection,
                cost_config=cost_config,
            )

        if not account.trade_allowed or not account.terminal_trade_allowed:
            return BrokerSubmissionResult(
                outcome=BrokerSubmissionOutcome.REJECTED,
                rejection=BrokerRejection(
                    code=BrokerRejectionCode.TRADING_DISABLED,
                    message="terminal trading is disabled for this account",
                ),
                cost_config=cost_config,
            )

        order = _execution_order(request, deviation=self._slippage_deviation)
        submit_outcome = self._client.submit(request.order_id, order)
        outcome = str(submit_outcome.get("outcome", ""))

        if outcome == "rejected":
            reason = str(submit_outcome.get("reason", "order rejected by edge"))
            return BrokerSubmissionResult(
                outcome=BrokerSubmissionOutcome.REJECTED,
                rejection=BrokerRejection(
                    code=BrokerRejectionCode.ORDER_SEND_FAILED,
                    message=reason,
                    context={"retcode": submit_outcome.get("retcode")},
                ),
                raw_response=submit_outcome,
                cost_config=cost_config,
            )

        if outcome != "accepted":
            return BrokerSubmissionResult(
                outcome=BrokerSubmissionOutcome.UNKNOWN,
                rejection=BrokerRejection(
                    code=BrokerRejectionCode.ORDER_SEND_FAILED,
                    message=str(submit_outcome.get("reason", "indeterminate submit outcome")),
                ),
                raw_response=submit_outcome,
                cost_config=cost_config,
            )

        external_order_id = str(submit_outcome.get("order_ticket", "")) or None
        lookup = self._lookup_state(request, external_order_id=external_order_id)
        if lookup.status == BrokerOrderLookupStatus.FILLED and lookup.fill is not None:
            return BrokerSubmissionResult(
                outcome=BrokerSubmissionOutcome.FILLED,
                fill=lookup.fill,
                external_order_id=external_order_id,
                external_deal_ids=lookup.external_deal_ids,
                raw_response={"submit": submit_outcome, "lookup": "filled"},
                cost_config=cost_config,
            )

        return BrokerSubmissionResult(
            outcome=BrokerSubmissionOutcome.UNKNOWN,
            external_order_id=external_order_id,
            raw_response={"submit": submit_outcome, "lookup": lookup.status.value},
            cost_config=cost_config,
        )

    def lookup_order(self, request: MarketOrderRequest) -> BrokerOrderState:
        return self._lookup_state(request)
