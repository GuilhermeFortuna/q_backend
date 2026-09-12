"""Resolve UNKNOWN / ReconciliationState.PENDING orders.

An order enters ``UNKNOWN`` with ``ReconciliationState.PENDING`` whenever a crash
window or a fill-persistence failure makes its broker outcome ambiguous. This
module is the *only* reader of that state. It asks the broker what happened and
records the answer; it never re-submits.

State machine (order ``status`` / ``reconciliation_state``)::

    UNKNOWN/PENDING --broker filled--> FILLED/RECONCILED   (ledger applied)
    UNKNOWN/PENDING --broker rejected/not-found--> REJECTED/RECONCILED (ledger untouched)
    UNKNOWN/PENDING --broker unavailable--> UNKNOWN/PENDING (attempt recorded, still blocking)

Automatic resolution is performed by the worker (actor ``reconciler``). Operators
may resolve manually through the API (actor is the operator id); the manual path
requires an explicit outcome — there is no "assume it's fine" default.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Callable, Optional
from uuid import UUID

from sqlalchemy.orm import Session

from q_backend.execution.brokers.base import (
    BrokerOrderLookupStatus,
    ExecutionBroker,
    MarketOrderRequest,
)
from q_backend.execution.domain import (
    BrokerMode,
    DecisionOutcome,
    ExecutionOrderStatus,
    ExecutionSide,
    FillRecord,
    ReconciliationState,
)
from q_backend.execution.ledger import ExecutionLedger
from q_backend.storage.db.execution_models import ExecutionDeployment, ExecutionOrder
from q_backend.storage.db.execution_repositories import (
    finalize_order_reconciliation,
    get_execution_deployment,
    list_pending_reconciliation_orders,
    record_reconciliation_attempt,
    transition_execution_order,
    update_execution_decision_outcome,
)

AUTO_ACTOR = "reconciler"


class OrderNotPendingError(ValueError):
    """Raised when a manual resolution targets an order that is not pending."""


class ReconciliationResolution(str, Enum):
    FILLED = "filled"
    FAILED = "failed"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class ReconciliationOutcome:
    order_id: UUID
    deployment_id: UUID
    resolution: ReconciliationResolution
    message: str = ""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def lookup_request_for_order(order: ExecutionOrder, deployment: ExecutionDeployment) -> MarketOrderRequest:
    broker_mode = BrokerMode(order.broker_mode)
    return MarketOrderRequest(
        deployment_id=deployment.id,
        order_id=order.id,
        symbol=deployment.symbol,
        side=ExecutionSide(order.side),
        quantity=order.quantity,
        external_fill_id=f"{broker_mode.value}:{order.id}",
    )


def _assert_pending(order: ExecutionOrder) -> None:
    if (
        order.status != ExecutionOrderStatus.UNKNOWN.value
        or order.reconciliation_state != ReconciliationState.PENDING.value
    ):
        raise OrderNotPendingError(
            f"order {order.id} is not awaiting reconciliation "
            f"(status={order.status}, reconciliation_state={order.reconciliation_state})"
        )


def apply_filled_resolution(
    session: Session,
    *,
    order: ExecutionOrder,
    deployment: ExecutionDeployment,
    fill: FillRecord,
    ledger: ExecutionLedger,
    point_value: Decimal,
    reconciled_by: str,
    detail: Optional[str],
    at: Optional[datetime] = None,
) -> None:
    """Apply a confirmed fill through the same atomic ledger path as the happy path."""
    ts = at or _utcnow()
    ledger.apply_fill(
        session,
        paper_account_id=deployment.paper_account_id,
        deployment_id=deployment.id,
        order_id=order.id,
        fill=fill,
        point_value=point_value,
        symbol=deployment.symbol,
        reconciling=True,
    )
    finalize_order_reconciliation(session, order.id, reconciled_by=reconciled_by, detail=detail, at=ts)
    if order.decision_id is not None:
        update_execution_decision_outcome(session, order.decision_id, outcome=DecisionOutcome.ORDER_FILLED)


def apply_failed_resolution(
    session: Session,
    *,
    order: ExecutionOrder,
    reconciled_by: str,
    detail: Optional[str],
    reason: str,
    at: Optional[datetime] = None,
) -> None:
    """Fail the order and release its intent; the ledger is left untouched."""
    ts = at or _utcnow()
    transition_execution_order(
        session,
        order.id,
        ExecutionOrderStatus.REJECTED,
        rejection_reason=reason,
        completed_at=ts,
    )
    finalize_order_reconciliation(session, order.id, reconciled_by=reconciled_by, detail=detail, at=ts)
    if order.decision_id is not None:
        update_execution_decision_outcome(session, order.decision_id, outcome=DecisionOutcome.ORDER_REJECTED)


def resolve_order_manually(
    session: Session,
    *,
    order: ExecutionOrder,
    deployment: ExecutionDeployment,
    outcome: str,
    actor: str,
    note: str,
    ledger: ExecutionLedger,
    point_value: Decimal,
    fill: Optional[FillRecord] = None,
    at: Optional[datetime] = None,
) -> ReconciliationOutcome:
    """Operator-asserted resolution. ``outcome`` must be 'filled' or 'not_filled'."""
    _assert_pending(order)
    if outcome == "filled":
        if fill is None:
            raise ValueError("filled resolution requires fill details")
        apply_filled_resolution(
            session,
            order=order,
            deployment=deployment,
            fill=fill,
            ledger=ledger,
            point_value=point_value,
            reconciled_by=actor,
            detail=note,
            at=at,
        )
        return ReconciliationOutcome(
            order_id=order.id,
            deployment_id=deployment.id,
            resolution=ReconciliationResolution.FILLED,
            message="manual resolution: filled",
        )
    if outcome == "not_filled":
        apply_failed_resolution(
            session,
            order=order,
            reconciled_by=actor,
            detail=note,
            reason=f"manual resolution: not filled ({note})",
            at=at,
        )
        return ReconciliationOutcome(
            order_id=order.id,
            deployment_id=deployment.id,
            resolution=ReconciliationResolution.FAILED,
            message="manual resolution: not filled",
        )
    raise ValueError(f"unknown manual resolution outcome: {outcome!r}")


class OrderReconciler:
    """Reads pending unknowns and resolves them against a broker (never resends)."""

    def __init__(
        self,
        *,
        broker: ExecutionBroker,
        ledger: ExecutionLedger,
        point_value: Decimal,
        clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self._broker = broker
        self._ledger = ledger
        self._point_value = point_value
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def reconcile_deployment(self, session: Session, deployment: ExecutionDeployment) -> list[ReconciliationOutcome]:
        outcomes = []
        for order in list_pending_reconciliation_orders(session, deployment_id=deployment.id):
            outcomes.append(self._reconcile_order(session, order, deployment))
        return outcomes

    def reconcile_all_pending(self, session: Session) -> list[ReconciliationOutcome]:
        deployments: dict[UUID, Optional[ExecutionDeployment]] = {}
        outcomes: list[ReconciliationOutcome] = []
        for order in list_pending_reconciliation_orders(session):
            deployment = deployments.get(order.deployment_id)
            if order.deployment_id not in deployments:
                deployment = get_execution_deployment(session, order.deployment_id)
                deployments[order.deployment_id] = deployment
            if deployment is None:
                continue
            outcomes.append(self._reconcile_order(session, order, deployment))
        return outcomes

    def _reconcile_order(
        self,
        session: Session,
        order: ExecutionOrder,
        deployment: ExecutionDeployment,
    ) -> ReconciliationOutcome:
        now = self._clock()
        state = self._broker.lookup_order(lookup_request_for_order(order, deployment))

        if state.status == BrokerOrderLookupStatus.FILLED:
            if state.fill is None:
                record_reconciliation_attempt(
                    session,
                    order.id,
                    at=now,
                    error="broker reported filled without fill details",
                )
                return ReconciliationOutcome(
                    order_id=order.id,
                    deployment_id=deployment.id,
                    resolution=ReconciliationResolution.UNAVAILABLE,
                    message="filled without fill details",
                )
            apply_filled_resolution(
                session,
                order=order,
                deployment=deployment,
                fill=state.fill,
                ledger=self._ledger,
                point_value=self._point_value,
                reconciled_by=AUTO_ACTOR,
                detail=state.message or "broker confirmed fill",
                at=now,
            )
            return ReconciliationOutcome(
                order_id=order.id,
                deployment_id=deployment.id,
                resolution=ReconciliationResolution.FILLED,
                message=state.message,
            )

        if state.status in (
            BrokerOrderLookupStatus.REJECTED,
            BrokerOrderLookupStatus.NOT_FOUND,
        ):
            reason = f"reconciliation: broker reported {state.status.value}"
            apply_failed_resolution(
                session,
                order=order,
                reconciled_by=AUTO_ACTOR,
                detail=state.message or reason,
                reason=reason,
                at=now,
            )
            return ReconciliationOutcome(
                order_id=order.id,
                deployment_id=deployment.id,
                resolution=ReconciliationResolution.FAILED,
                message=state.message,
            )

        # UNAVAILABLE: fail closed. Record the attempt and keep blocking.
        record_reconciliation_attempt(
            session,
            order.id,
            at=now,
            error=state.message or "broker unavailable",
        )
        return ReconciliationOutcome(
            order_id=order.id,
            deployment_id=deployment.id,
            resolution=ReconciliationResolution.UNAVAILABLE,
            message=state.message,
        )
