"""MT5 live broker adapter that routes through the execution edge (Q-042)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Literal, Optional
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
    CancelOrderRequest,
    Clock,
    MarketOrderRequest,
    PaperCostConfig,
    PendingOrderRequest,
    ProtectionRequest,
)
from q_backend.execution.brokers.live_gates import (
    LiveExecutionGates,
    evaluate_account_gates,
    evaluate_controlled_account_gate,
    evaluate_live_gates,
)
from q_backend.execution.brokers.mt5_constants import retcode_label
from q_backend.execution.domain import BrokerMode, ExecutionOrderType, ExecutionSide, FillRecord
from q_backend.execution.edge_client import EdgeClient, EdgeUnavailable

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MT5OrdersAccount:
    login: int
    server: str
    trade_mode: Optional[str]


@dataclass(frozen=True)
class MT5OrdersStatus:
    """Whether MT5 orders are possible on the connected account, for health reporting."""

    capability: Literal["available", "validation_required", "locked"]
    reason: Optional[str]
    account: Optional[MT5OrdersAccount]


def mt5_order_status(client: EdgeClient, gates: LiveExecutionGates) -> MT5OrdersStatus:
    """Answer whether an MT5 deployment could trade on the connected account.

    The deployment flag belongs to each deployment, so it is not evaluated here.
    """
    try:
        account = client.account()
    except EdgeUnavailable as exc:
        return MT5OrdersStatus(capability="locked", reason=str(exc), account=None)
    reported = MT5OrdersAccount(login=account.login, server=account.server, trade_mode=account.trade_mode)
    rejection = evaluate_account_gates(gates, account_login=account.login)
    if rejection is not None:
        return MT5OrdersStatus(capability="locked", reason=rejection.message, account=reported)
    rejection = evaluate_controlled_account_gate(gates, trade_mode=account.trade_mode)
    if rejection is not None:
        return MT5OrdersStatus(capability="validation_required", reason=rejection.message, account=reported)
    return MT5OrdersStatus(capability="available", reason=None, account=reported)


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
        sl=float(request.stop_loss) if request.stop_loss is not None else None,
        tp=float(request.take_profit) if request.take_profit is not None else None,
    )


_PENDING_TYPES = {
    (ExecutionSide.BUY, ExecutionOrderType.LIMIT): 2,
    (ExecutionSide.SELL, ExecutionOrderType.LIMIT): 3,
    (ExecutionSide.BUY, ExecutionOrderType.STOP): 4,
    (ExecutionSide.SELL, ExecutionOrderType.STOP): 5,
}


def _pending_execution_order(request: PendingOrderRequest, *, deviation: int) -> ExecutionOrder:
    expiration = None
    if request.expires_at is not None:
        expiration = int(_as_utc(request.expires_at).timestamp())
    return ExecutionOrder(
        symbol=request.symbol,
        volume=float(request.quantity),
        side="buy" if request.side == ExecutionSide.BUY else "sell",
        type=_PENDING_TYPES[(request.side, request.order_type)],
        price=float(request.price),
        deviation=deviation,
        sl=float(request.stop_loss) if request.stop_loss is not None else None,
        tp=float(request.take_profit) if request.take_profit is not None else None,
        expiration=expiration,
    )


def _edge_version(schema_version: str) -> tuple[int, int]:
    major, _, minor = schema_version.partition(".")
    try:
        return int(major), int(minor or 0)
    except ValueError:
        return (0, 0)


def _unsupported_edge(kind: str, schema_version: str) -> BrokerSubmissionResult:
    return BrokerSubmissionResult(
        outcome=BrokerSubmissionOutcome.REJECTED,
        rejection=BrokerRejection(
            code=BrokerRejectionCode.UNSUPPORTED_ORDER_KIND,
            message=f"execution edge {schema_version} cannot place {kind}; 1.2 or later is required",
        ),
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

    def _precheck(
        self,
        *,
        live_activation_enabled: bool,
        cost_config: PaperCostConfig,
    ) -> Optional[BrokerSubmissionResult]:
        """Gates and account checks shared by every edge action. None means it may proceed."""
        gates = LiveExecutionGates(
            enabled=self._gates.enabled,
            account_allowlist=self._gates.account_allowlist,
            deployment_live_activation_enabled=live_activation_enabled,
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

        gate_rejection = evaluate_live_gates(
            gates,
            account_login=account.login,
            trade_mode=account.trade_mode,
        )
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
        return None

    def submit_market_order(
        self,
        request: MarketOrderRequest,
        *,
        cost_config: PaperCostConfig,
    ) -> BrokerSubmissionResult:
        rejection = self._precheck(
            live_activation_enabled=request.live_activation_enabled,
            cost_config=cost_config,
        )
        if rejection is not None:
            return rejection

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

    def _edge_gap(self) -> Optional[str]:
        """Why this edge cannot place pending or protective orders, or None when it can."""
        try:
            health = self._client.health()
        except EdgeUnavailable as exc:
            return str(exc)
        if _edge_version(health.schema_version) < _PENDING_MIN_EDGE:
            return health.schema_version
        return None

    def submit_pending_order(self, request: PendingOrderRequest) -> BrokerSubmissionResult:
        rejection = self._precheck(live_activation_enabled=request.live_activation_enabled, cost_config=_NO_COST)
        if rejection is not None:
            return rejection
        gap = self._edge_gap()
        if gap is not None:
            return _unsupported_edge("stop or limit orders", gap)

        order = _pending_execution_order(request, deviation=self._slippage_deviation)
        outcome = self._client.submit_pending(request.order_id, order)
        kind = str(outcome.get("outcome", ""))
        if kind == "accepted":
            ticket = str(outcome.get("order_ticket", "")) or None
            return BrokerSubmissionResult(
                outcome=BrokerSubmissionOutcome.WORKING,
                external_order_id=ticket,
                raw_response=outcome,
            )
        if kind == "rejected":
            return BrokerSubmissionResult(
                outcome=BrokerSubmissionOutcome.REJECTED,
                rejection=BrokerRejection(
                    code=BrokerRejectionCode.ORDER_SEND_FAILED,
                    message=str(outcome.get("reason", "pending order rejected by edge")),
                    context={"retcode": outcome.get("retcode")},
                ),
                raw_response=outcome,
            )
        return BrokerSubmissionResult(
            outcome=BrokerSubmissionOutcome.UNKNOWN,
            rejection=BrokerRejection(
                code=BrokerRejectionCode.ORDER_SEND_FAILED,
                message=str(outcome.get("reason", "indeterminate pending submit outcome")),
            ),
            raw_response=outcome,
        )

    def lookup_pending_order(self, request: PendingOrderRequest) -> BrokerOrderState:
        window_start, window_end = self._lookup_window(request)
        try:
            outcome = self._client.lookup(request.order_id, window_start, window_end, kind="pending")
        except EdgeUnavailable as exc:
            return BrokerOrderState(status=BrokerOrderLookupStatus.UNAVAILABLE, message=str(exc))

        result = str(outcome.get("outcome", ""))
        if result == "placed":
            ticket = str(outcome.get("order_ticket", "")) or None
            return BrokerOrderState(
                status=BrokerOrderLookupStatus.WORKING,
                external_order_id=ticket,
                message="pending order rests at the broker",
            )
        if result == "filled":
            reconciled = _fill_from_deals(
                request,
                outcome.get("deals") or [],
                external_order_id=None,
                now=self._clock.now(),
            )
            if reconciled is None:
                return BrokerOrderState(
                    status=BrokerOrderLookupStatus.UNAVAILABLE,
                    message="edge reported filled without usable deal details",
                )
            fill, deal_ids = reconciled
            position_id = _position_id_from_deals(outcome.get("deals") or [])
            return BrokerOrderState(
                status=BrokerOrderLookupStatus.FILLED,
                fill=fill,
                external_position_id=position_id,
                external_deal_ids=deal_ids,
                message="reconciled from edge deal history",
            )
        if result == "cancelled":
            return BrokerOrderState(status=BrokerOrderLookupStatus.CANCELLED, message=str(outcome.get("reason", "")))
        if result == "expired":
            return BrokerOrderState(status=BrokerOrderLookupStatus.EXPIRED, message="pending order expired")
        if result == "rejected":
            reason = outcome.get("reason") or retcode_label(int(outcome.get("retcode", 0)))
            return BrokerOrderState(status=BrokerOrderLookupStatus.REJECTED, message=str(reason))
        if result == "not_found":
            return BrokerOrderState(
                status=BrokerOrderLookupStatus.NOT_FOUND,
                message="no edge order or deal matches the intent",
            )
        return BrokerOrderState(
            status=BrokerOrderLookupStatus.UNAVAILABLE,
            message=str(outcome.get("reason", "edge lookup unavailable")),
        )

    def set_protection(self, request: ProtectionRequest) -> BrokerSubmissionResult:
        rejection = self._precheck(live_activation_enabled=request.live_activation_enabled, cost_config=_NO_COST)
        if rejection is not None:
            return rejection
        gap = self._edge_gap()
        if gap is not None:
            return _unsupported_edge("protective levels", gap)

        outcome = self._client.submit_protection(
            request.order_id,
            symbol=request.symbol,
            position=int(request.position_ticket),
            stop_loss=float(request.stop_loss) if request.stop_loss is not None else 0.0,
            take_profit=float(request.take_profit) if request.take_profit is not None else 0.0,
        )
        return _applied_or_rejected(
            outcome, BrokerSubmissionOutcome.APPLIED, external_position_id=request.position_ticket
        )

    def cancel_order(self, request: CancelOrderRequest) -> BrokerSubmissionResult:
        rejection = self._precheck(live_activation_enabled=request.live_activation_enabled, cost_config=_NO_COST)
        if rejection is not None:
            return rejection
        gap = self._edge_gap()
        if gap is not None:
            return _unsupported_edge("cancelled working orders", gap)

        outcome = self._client.submit_remove(
            request.order_id,
            symbol=request.symbol,
            order_ticket=int(request.external_order_id),
        )
        return _applied_or_rejected(
            outcome, BrokerSubmissionOutcome.CANCELLED, external_order_id=request.external_order_id
        )

    def lookup_protection(self, request: ProtectionRequest) -> BrokerOrderState:
        """Read-only check that a position already carries the requested levels."""
        try:
            positions = self._client.positions(request.symbol)
        except EdgeUnavailable as exc:
            return BrokerOrderState(status=BrokerOrderLookupStatus.UNAVAILABLE, message=str(exc))
        row = next((p for p in positions if str(p.get("ticket")) == request.position_ticket), None)
        if row is None:
            return BrokerOrderState(
                status=BrokerOrderLookupStatus.UNAVAILABLE,
                message="position is no longer open; its levels cannot be verified",
            )
        if _same_level(row.get("sl"), request.stop_loss) and _same_level(row.get("tp"), request.take_profit):
            return BrokerOrderState(
                status=BrokerOrderLookupStatus.FILLED,
                external_position_id=request.position_ticket,
                message="position carries the requested levels",
            )
        return BrokerOrderState(
            status=BrokerOrderLookupStatus.NOT_FOUND,
            external_position_id=request.position_ticket,
            message="position does not carry the requested levels",
        )

    def lookup_cancellation(self, request: CancelOrderRequest) -> BrokerOrderState:
        """Read-only check that a working order has left the terminal's order list."""
        try:
            working = self._client.orders(request.symbol)
        except EdgeUnavailable as exc:
            return BrokerOrderState(status=BrokerOrderLookupStatus.UNAVAILABLE, message=str(exc))
        if any(str(row.get("ticket")) == request.external_order_id for row in working):
            return BrokerOrderState(
                status=BrokerOrderLookupStatus.NOT_FOUND,
                external_order_id=request.external_order_id,
                message="pending order still rests at the broker",
            )
        return BrokerOrderState(
            status=BrokerOrderLookupStatus.CANCELLED,
            external_order_id=request.external_order_id,
            message="pending order no longer rests at the broker",
        )


_PENDING_MIN_EDGE = (1, 2)
_NO_COST = PaperCostConfig()


def _same_level(value: Any, expected: Optional[Decimal]) -> bool:
    if expected is None or float(expected) == 0.0:
        return value in (None, 0, 0.0)
    return value is not None and abs(float(value) - float(expected)) < 1e-9


def _position_id_from_deals(deals: list[dict[str, Any]]) -> Optional[str]:
    for deal in deals:
        if deal.get("position_id") is not None:
            return str(int(deal["position_id"]))
    return None


def _applied_or_rejected(
    outcome: dict[str, Any],
    accepted: BrokerSubmissionOutcome,
    *,
    external_order_id: Optional[str] = None,
    external_position_id: Optional[str] = None,
) -> BrokerSubmissionResult:
    kind = str(outcome.get("outcome", ""))
    if kind == "accepted":
        return BrokerSubmissionResult(
            outcome=accepted,
            external_order_id=external_order_id,
            external_position_id=external_position_id,
            raw_response=outcome,
        )
    if kind == "rejected":
        return BrokerSubmissionResult(
            outcome=BrokerSubmissionOutcome.REJECTED,
            rejection=BrokerRejection(
                code=BrokerRejectionCode.PROTECTIVE_LEVEL_REJECTED,
                message=str(outcome.get("reason", "edge rejected the request")),
                context={"retcode": outcome.get("retcode")},
            ),
            raw_response=outcome,
        )
    return BrokerSubmissionResult(
        outcome=BrokerSubmissionOutcome.UNKNOWN,
        rejection=BrokerRejection(
            code=BrokerRejectionCode.ORDER_SEND_FAILED,
            message=str(outcome.get("reason", "indeterminate edge outcome")),
        ),
        raw_response=outcome,
    )
