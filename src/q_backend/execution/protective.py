"""Working stop and limit entries, protective levels and broker-side exits (Q-137).

The forward evaluator decides what a bar asks for. This module puts those decisions at the
broker and keeps the ledger in step with what the broker reports:

* a resting stop or limit entry is placed as a ``pending`` order, replaced by removing the
  working one first, and cancelled on request, on pause, on kill switch, on flatten or at the
  day-trade close;
* ``amend`` moves the levels of the open position;
* fills, expiries, cancellations and rejections of working orders, and exits the broker made
  on its own (stop-loss, take-profit or stop-out), are reconciled before each decision and
  between bars.

Every broker call follows the intent rules: the intent row is committed before dispatch, a
transport failure is ``unknown`` and is resolved by read-only lookups, and nothing is retried.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any, Callable, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from q_backend.execution.brokers.base import (
    BrokerOrderLookupStatus,
    BrokerOrderState,
    BrokerSubmissionOutcome,
    BrokerSubmissionResult,
    CancelOrderRequest,
    ExecutionBroker,
    MarketOrderRequest,
    PaperCostConfig,
    PendingOrderRequest,
    PositionExitRequest,
    ProtectionRequest,
)
from q_backend.execution.domain import (
    BrokerMode,
    DeploymentLifecycle,
    ExecutionIntentKind,
    ExecutionOrderStatus,
    ExecutionOrderType,
    ExecutionSide,
    PositionSide,
    ReconciliationState,
    RiskRejection,
    RiskRejectionCode,
)
from q_backend.execution.ledger import ExecutionLedger
from q_backend.execution.results import ForwardDecisionResult
from q_backend.storage.db.execution_models import ExecutionNetPosition, ExecutionOrder
from q_backend.storage.db.execution_repositories import (
    create_execution_order_intent,
    get_open_net_position,
    list_working_orders,
    mark_execution_dispatch_attempt,
    record_risk_event,
    set_open_position_protection,
    set_open_position_timed_exit,
    transition_deployment_lifecycle,
    transition_execution_order,
)

logger = logging.getLogger(__name__)

RiskCheck = Callable[..., Optional[RiskRejection]]

_WORKING_ENTRY = (ExecutionOrderStatus.WORKING.value, ExecutionOrderStatus.UNKNOWN.value)


def _opposite(side: ExecutionSide) -> ExecutionSide:
    return ExecutionSide.SELL if side == ExecutionSide.BUY else ExecutionSide.BUY


def _position_side_as_execution(side: PositionSide) -> ExecutionSide:
    return ExecutionSide.BUY if side == PositionSide.LONG else ExecutionSide.SELL


class ProtectiveOrders:
    """Apply working-order decisions and reconcile them with the broker and the ledger."""

    def __init__(
        self,
        *,
        broker: ExecutionBroker,
        ledger: ExecutionLedger,
        clock: Callable[[], datetime],
        commit: Callable[[Session], None],
        risk_check: RiskCheck,
    ) -> None:
        self._broker = broker
        self._ledger = ledger
        self._clock = clock
        self._commit = commit
        self._risk_check = risk_check

    # ------------------------------------------------------------------ decisions

    def apply_bar(
        self,
        session: Session,
        *,
        deployment: Any,
        eval_result: ForwardDecisionResult,
        decision_id: uuid.UUID,
        lease_token: str,
        worker_id: str,
        cost_config: Any,
        point_value: Decimal,
    ) -> None:
        """Apply the management a bar carries: cancellations, then amendments, then placements."""
        if eval_result.cancel_pending:
            self.remove_working(session, deployment=deployment, worker_id=worker_id, reason="strategy_cancel")
        if eval_result.protect_levels is not None:
            self.amend_position(
                session,
                deployment=deployment,
                levels=eval_result.protect_levels,
                decision_id=decision_id,
                worker_id=worker_id,
            )
        self.place_entries(
            session,
            deployment=deployment,
            eval_result=eval_result,
            decision_id=decision_id,
            lease_token=lease_token,
            worker_id=worker_id,
            cost_config=cost_config,
            point_value=point_value,
        )
        if eval_result.timed_exit_at is not None and get_open_net_position(session, deployment.id) is not None:
            set_open_position_timed_exit(session, deployment.id, exit_at=eval_result.timed_exit_at)
            self._commit(session)

    def place_entries(
        self,
        session: Session,
        *,
        deployment: Any,
        eval_result: ForwardDecisionResult,
        decision_id: Optional[uuid.UUID],
        lease_token: str,
        worker_id: str,
        cost_config: Any,
        point_value: Decimal,
    ) -> Optional[str]:
        """Place the bar's resting entry or OCO group, if it has one. Returns the outcome."""
        quantity = Decimal(str(eval_result.requested_quantity or 0))
        if eval_result.pending_entry is not None:
            return self.place_pending(
                session,
                deployment=deployment,
                spec=eval_result.pending_entry,
                quantity=quantity,
                bar_close_time=eval_result.bar_close_time,
                decision_id=decision_id,
                lease_token=lease_token,
                worker_id=worker_id,
                cost_config=cost_config,
                point_value=point_value,
            )
        if eval_result.pending_group is not None:
            return self.place_group(
                session,
                deployment=deployment,
                legs=eval_result.pending_group,
                quantity=quantity,
                bar_close_time=eval_result.bar_close_time,
                decision_id=decision_id,
                lease_token=lease_token,
                worker_id=worker_id,
                cost_config=cost_config,
                point_value=point_value,
            )
        return None

    def place_pending(
        self,
        session: Session,
        *,
        deployment: Any,
        spec: dict[str, Any],
        quantity: Decimal,
        bar_close_time: datetime,
        decision_id: Optional[uuid.UUID],
        lease_token: str,
        worker_id: str,
        cost_config: Any,
        point_value: Decimal,
    ) -> str:
        """Replace the working entries with one resting stop or limit order. Returns the outcome."""
        replaced = self._replace_working(session, deployment=deployment, worker_id=worker_id)
        if replaced != "clear":
            return replaced
        return self._place_one(
            session,
            deployment=deployment,
            spec=spec,
            quantity=quantity,
            bar_close_time=bar_close_time,
            decision_id=decision_id,
            lease_token=lease_token,
            worker_id=worker_id,
            cost_config=cost_config,
            point_value=point_value,
            group_id=None,
        )

    def place_group(
        self,
        session: Session,
        *,
        deployment: Any,
        legs: tuple[dict[str, Any], ...],
        quantity: Decimal,
        bar_close_time: datetime,
        decision_id: Optional[uuid.UUID],
        lease_token: str,
        worker_id: str,
        cost_config: Any,
        point_value: Decimal,
    ) -> str:
        """Place an OCO group: one resting entry per leg, tagged with a shared group id.

        A leg the broker does not accept takes the placed legs down with it, so the group
        never rests partly.
        """
        replaced = self._replace_working(session, deployment=deployment, worker_id=worker_id)
        if replaced != "clear":
            return replaced
        group_id = uuid.uuid4().hex
        placed: list[ExecutionOrder] = []
        for leg in legs:
            outcome = self._place_one(
                session,
                deployment=deployment,
                spec=leg,
                quantity=quantity,
                bar_close_time=bar_close_time,
                decision_id=decision_id,
                lease_token=lease_token,
                worker_id=worker_id,
                cost_config=cost_config,
                point_value=point_value,
                group_id=group_id,
            )
            if outcome != "working":
                for sibling in placed:
                    self._cancel(session, deployment, sibling, worker_id, reason="oco_leg_refused")
                return outcome
            placed.append(self._latest_entry(session, deployment.id, group_id))
        return "working"

    def _replace_working(self, session: Session, *, deployment: Any, worker_id: str) -> str:
        """Remove the working entries a new placement replaces; ``clear`` when none is left."""
        if get_open_net_position(session, deployment.id) is not None:
            return "position_open"
        for working in list_working_orders(session, deployment.id):
            if working.status == ExecutionOrderStatus.UNKNOWN.value:
                # Its fate is not known yet; placing a second order now could double the entry.
                return "unresolved_working_order"
            outcome = self._cancel(session, deployment, working, worker_id, reason="replaced")
            if outcome != "cancelled":
                return f"replace_{outcome}"
        return "clear"

    def _place_one(
        self,
        session: Session,
        *,
        deployment: Any,
        spec: dict[str, Any],
        quantity: Decimal,
        bar_close_time: datetime,
        decision_id: Optional[uuid.UUID],
        lease_token: str,
        worker_id: str,
        cost_config: Any,
        point_value: Decimal,
        group_id: Optional[str],
    ) -> str:
        if quantity <= 0:
            return "invalid_quantity"
        side = ExecutionSide(spec["side"])
        order_type = ExecutionOrderType(spec["order_type"])
        now = self._clock()
        expires_at = spec.get("expires_at")
        rejection = self._risk_check(
            session,
            deployment=deployment,
            side=side,
            quantity=quantity,
            bar_close_time=bar_close_time,
            lease_token=lease_token,
            worker_id=worker_id,
            cost_config=cost_config,
            point_value=point_value,
        )
        if rejection is not None:
            return "risk_rejected"

        details: dict[str, Any] = {"reason": spec.get("reason"), "bar_close_time": bar_close_time.isoformat()}
        if group_id is not None:
            details["oco_group"] = group_id
        order = create_execution_order_intent(
            session,
            deployment_id=deployment.id,
            decision_id=decision_id,
            broker_mode=BrokerMode(deployment.broker_mode),
            side=side,
            quantity=quantity,
            order_type=order_type,
            intent_kind=ExecutionIntentKind.ENTRY,
            price=optional_decimal(spec.get("price")),
            stop_loss=optional_decimal(spec.get("stop_loss")),
            take_profit=optional_decimal(spec.get("take_profit")),
            expires_at=expires_at,
            metadata=details,
            intent_committed_at=now,
            producer=worker_id,
        )
        session.flush()
        self._commit(session)
        mark_execution_dispatch_attempt(session, order.id, attempted_at=self._clock(), producer=worker_id)
        self._commit(session)

        broker_mode = BrokerMode(deployment.broker_mode)
        try:
            result = self._broker.submit_pending_order(
                PendingOrderRequest(
                    deployment_id=deployment.id,
                    order_id=order.id,
                    symbol=deployment.symbol,
                    side=side,
                    order_type=order_type,
                    quantity=quantity,
                    price=order.price,
                    stop_loss=order.stop_loss,
                    take_profit=order.take_profit,
                    expires_at=expires_at,
                    intent_created_at=order.intent_committed_at or now,
                    broker_mode=broker_mode,
                    live_activation_enabled=bool(deployment.live_activation_enabled),
                )
            )
        except Exception as exc:  # noqa: BLE001 - an ambiguous send must not be retried
            logger.exception("pending submit raised for order %s", order.id)
            result = BrokerSubmissionResult(
                outcome=BrokerSubmissionOutcome.UNKNOWN,
                rejection=None,
                metadata={"exception": str(exc)},
            )
        return self._record_pending_result(session, order, result, now=self._clock(), worker_id=worker_id)

    def _latest_entry(self, session: Session, deployment_id: uuid.UUID, group_id: str) -> ExecutionOrder:
        members = self._group_members(session, deployment_id, group_id)
        return members[-1]

    def _group_members(self, session: Session, deployment_id: uuid.UUID, group_id: str) -> list[ExecutionOrder]:
        rows = session.execute(
            select(ExecutionOrder)
            .where(
                ExecutionOrder.deployment_id == deployment_id,
                ExecutionOrder.intent_kind == ExecutionIntentKind.ENTRY.value,
            )
            .order_by(ExecutionOrder.created_at)
        ).scalars()
        return [row for row in rows if (row.details or {}).get("oco_group") == group_id]

    def _record_pending_result(
        self,
        session: Session,
        order: ExecutionOrder,
        result: BrokerSubmissionResult,
        *,
        now: datetime,
        worker_id: str,
    ) -> str:
        if result.outcome == BrokerSubmissionOutcome.WORKING:
            transition_execution_order(
                session, order.id, ExecutionOrderStatus.SUBMITTED, submitted_at=now, producer=worker_id
            )
            transition_execution_order(
                session,
                order.id,
                ExecutionOrderStatus.WORKING,
                external_order_id=result.external_order_id,
                producer=worker_id,
            )
            self._commit(session)
            return "working"
        if result.outcome == BrokerSubmissionOutcome.REJECTED:
            transition_execution_order(
                session,
                order.id,
                ExecutionOrderStatus.REJECTED,
                rejection_reason=_reason(result, "pending order rejected"),
                completed_at=now,
                producer=worker_id,
            )
            self._commit(session)
            return "rejected"
        transition_execution_order(
            session,
            order.id,
            ExecutionOrderStatus.UNKNOWN,
            reconciliation_state=ReconciliationState.PENDING,
            rejection_reason=_reason(result, "pending order outcome unknown"),
            external_order_id=result.external_order_id,
            producer=worker_id,
        )
        self._commit(session)
        return "unknown"

    def amend_position(
        self,
        session: Session,
        *,
        deployment: Any,
        levels: dict[str, Optional[float]],
        decision_id: Optional[uuid.UUID],
        worker_id: str,
    ) -> str:
        """Move the open position's levels. An amendment with one level keeps the other."""
        position = get_open_net_position(session, deployment.id)
        if position is None:
            return "no_position"
        stop_loss = optional_decimal(levels.get("stop_loss"))
        take_profit = optional_decimal(levels.get("take_profit"))
        stop_loss = stop_loss if stop_loss is not None else position.stop_loss
        take_profit = take_profit if take_profit is not None else position.take_profit
        if stop_loss is None and take_profit is None:
            return "no_levels"

        side = _position_side_as_execution(PositionSide(position.side))
        now = self._clock()
        order = create_execution_order_intent(
            session,
            deployment_id=deployment.id,
            decision_id=decision_id,
            broker_mode=BrokerMode(deployment.broker_mode),
            side=side,
            quantity=position.quantity,
            order_type=ExecutionOrderType.MARKET,
            intent_kind=ExecutionIntentKind.PROTECT,
            stop_loss=stop_loss,
            take_profit=take_profit,
            metadata={"position_id": str(position.id)},
            intent_committed_at=now,
            producer=worker_id,
        )
        session.flush()
        self._commit(session)
        mark_execution_dispatch_attempt(session, order.id, attempted_at=self._clock(), producer=worker_id)
        transition_execution_order(
            session, order.id, ExecutionOrderStatus.SUBMITTED, submitted_at=now, producer=worker_id
        )
        self._commit(session)

        try:
            result = self._broker.set_protection(
                ProtectionRequest(
                    deployment_id=deployment.id,
                    order_id=order.id,
                    symbol=deployment.symbol,
                    position_ticket=position.external_position_id or "",
                    stop_loss=stop_loss,
                    take_profit=take_profit,
                    intent_created_at=order.intent_committed_at or now,
                    broker_mode=BrokerMode(deployment.broker_mode),
                    live_activation_enabled=bool(deployment.live_activation_enabled),
                )
            )
        except Exception as exc:  # noqa: BLE001 - an ambiguous send must not be retried
            logger.exception("protection submit raised for order %s", order.id)
            result = BrokerSubmissionResult(outcome=BrokerSubmissionOutcome.UNKNOWN, metadata={"exception": str(exc)})

        return self._record_protection_result(
            session,
            order,
            result,
            position_id=position.id,
            stop_loss=stop_loss,
            take_profit=take_profit,
            worker_id=worker_id,
        )

    def _record_protection_result(
        self,
        session: Session,
        order: ExecutionOrder,
        result: BrokerSubmissionResult,
        *,
        position_id: uuid.UUID,
        stop_loss: Optional[Decimal],
        take_profit: Optional[Decimal],
        worker_id: str,
    ) -> str:
        now = self._clock()
        if result.outcome == BrokerSubmissionOutcome.APPLIED:
            transition_execution_order(
                session, order.id, ExecutionOrderStatus.FILLED, completed_at=now, producer=worker_id
            )
            position = session.get(ExecutionNetPosition, position_id)
            if position is not None and position.is_open:
                set_open_position_protection(
                    session,
                    position.deployment_id,
                    stop_loss=stop_loss,
                    take_profit=take_profit,
                )
            self._commit(session)
            return "applied"
        if result.outcome == BrokerSubmissionOutcome.REJECTED:
            reason = _reason(result, "protective levels rejected")
            transition_execution_order(
                session,
                order.id,
                ExecutionOrderStatus.REJECTED,
                rejection_reason=reason,
                completed_at=now,
                producer=worker_id,
            )
            record_risk_event(
                session,
                deployment_id=order.deployment_id,
                rejection_code=RiskRejectionCode.PROTECTIVE_LEVEL_REJECTED,
                message=reason,
                order_id=order.id,
                context={"stop_loss": _float_or_none(stop_loss), "take_profit": _float_or_none(take_profit)},
                producer=worker_id,
            )
            self._commit(session)
            return "rejected"
        transition_execution_order(
            session,
            order.id,
            ExecutionOrderStatus.UNKNOWN,
            reconciliation_state=ReconciliationState.PENDING,
            rejection_reason=_reason(result, "protective levels outcome unknown"),
            producer=worker_id,
        )
        self._commit(session)
        return "unknown"

    # ------------------------------------------------------------------ cancellation

    def remove_working(
        self,
        session: Session,
        *,
        deployment: Any,
        worker_id: str,
        reason: str,
    ) -> bool:
        """Remove every working entry that is not already in doubt. True when none is left working."""
        all_removed = True
        for working in list_working_orders(session, deployment.id):
            if working.status != ExecutionOrderStatus.WORKING.value:
                all_removed = False
                continue
            if self._cancel(session, deployment, working, worker_id, reason=reason) != "cancelled":
                all_removed = False
        return all_removed

    def _cancel(
        self,
        session: Session,
        deployment: Any,
        target: ExecutionOrder,
        worker_id: str,
        *,
        reason: str,
    ) -> str:
        if target.external_order_id is None:
            # The broker never named the order, so it may still rest there. Leave it to reconciliation.
            return "unresolved"

        now = self._clock()
        intent = create_execution_order_intent(
            session,
            deployment_id=deployment.id,
            decision_id=None,
            broker_mode=BrokerMode(deployment.broker_mode),
            side=ExecutionSide(target.side),
            quantity=target.quantity,
            order_type=ExecutionOrderType(target.order_type),
            intent_kind=ExecutionIntentKind.CANCEL,
            metadata={"target_order_id": str(target.id), "reason": reason},
            intent_committed_at=now,
            producer=worker_id,
        )
        session.flush()
        self._commit(session)
        mark_execution_dispatch_attempt(session, intent.id, attempted_at=self._clock(), producer=worker_id)
        transition_execution_order(
            session, intent.id, ExecutionOrderStatus.SUBMITTED, submitted_at=now, producer=worker_id
        )
        self._commit(session)

        try:
            result = self._broker.cancel_order(
                CancelOrderRequest(
                    deployment_id=deployment.id,
                    order_id=intent.id,
                    symbol=deployment.symbol,
                    external_order_id=target.external_order_id,
                    intent_created_at=intent.intent_committed_at or now,
                    broker_mode=BrokerMode(deployment.broker_mode),
                    live_activation_enabled=bool(deployment.live_activation_enabled),
                )
            )
        except Exception as exc:  # noqa: BLE001 - an ambiguous send must not be retried
            logger.exception("cancel submit raised for order %s", intent.id)
            result = BrokerSubmissionResult(outcome=BrokerSubmissionOutcome.UNKNOWN, metadata={"exception": str(exc)})

        done = self._clock()
        if result.outcome == BrokerSubmissionOutcome.CANCELLED:
            transition_execution_order(
                session, intent.id, ExecutionOrderStatus.CANCELLED, completed_at=done, producer=worker_id
            )
            transition_execution_order(
                session, target.id, ExecutionOrderStatus.CANCELLED, completed_at=done, producer=worker_id
            )
            self._commit(session)
            return "cancelled"
        if result.outcome == BrokerSubmissionOutcome.REJECTED:
            transition_execution_order(
                session,
                intent.id,
                ExecutionOrderStatus.REJECTED,
                rejection_reason=_reason(result, "cancel rejected"),
                completed_at=done,
                producer=worker_id,
            )
            self._commit(session)
            return "rejected"
        transition_execution_order(
            session,
            intent.id,
            ExecutionOrderStatus.UNKNOWN,
            reconciliation_state=ReconciliationState.PENDING,
            rejection_reason=_reason(result, "cancel outcome unknown"),
            producer=worker_id,
        )
        self._commit(session)
        return "unknown"

    # ------------------------------------------------------------------ reconciliation

    def reconcile(
        self,
        session: Session,
        *,
        deployment: Any,
        worker_id: str,
        point_value: Decimal,
        cost_config: Optional[PaperCostConfig] = None,
    ) -> None:
        """Bring working orders, unresolved management intents and broker-side exits up to date."""
        self._reconcile_working_entries(
            session,
            deployment=deployment,
            worker_id=worker_id,
            point_value=point_value,
            cost_config=cost_config or PaperCostConfig(),
        )
        self._reconcile_management_intents(session, deployment=deployment, worker_id=worker_id)
        self._reconcile_broker_exit(session, deployment=deployment, worker_id=worker_id, point_value=point_value)

    def _pending_request(self, deployment: Any, order: ExecutionOrder) -> PendingOrderRequest:
        return PendingOrderRequest(
            deployment_id=deployment.id,
            order_id=order.id,
            symbol=deployment.symbol,
            side=ExecutionSide(order.side),
            order_type=ExecutionOrderType(order.order_type),
            quantity=order.quantity,
            price=order.price,
            stop_loss=order.stop_loss,
            take_profit=order.take_profit,
            expires_at=order.expires_at,
            intent_created_at=order.intent_committed_at or self._clock(),
            broker_mode=BrokerMode(deployment.broker_mode),
            live_activation_enabled=bool(deployment.live_activation_enabled),
        )

    def _reconcile_working_entries(
        self,
        session: Session,
        *,
        deployment: Any,
        worker_id: str,
        point_value: Decimal,
        cost_config: PaperCostConfig,
    ) -> None:
        for order in list_working_orders(session, deployment.id):
            if order.status not in _WORKING_ENTRY:
                # Removed earlier in this pass, as the sibling of a filled OCO leg.
                continue
            state = self._broker.lookup_pending_order(self._pending_request(deployment, order))
            now = self._clock()
            if state.status == BrokerOrderLookupStatus.FILLED and state.fill is not None:
                group_id = (order.details or {}).get("oco_group")
                siblings = (
                    [m for m in self._group_members(session, deployment.id, group_id) if m.id != order.id]
                    if group_id
                    else []
                )
                first_filled = next((m for m in siblings if m.status == ExecutionOrderStatus.FILLED.value), None)
                self._apply_pending_fill(
                    session, deployment, order, state, point_value=point_value, worker_id=worker_id
                )
                if group_id and first_filled is not None:
                    self._contain_double_fill(
                        session,
                        deployment,
                        order,
                        intended=first_filled,
                        excess=state.fill.quantity,
                        excess_side=_opposite(state.fill.side),
                        cost_config=cost_config,
                        point_value=point_value,
                        worker_id=worker_id,
                    )
                elif group_id:
                    for sibling in siblings:
                        if sibling.status == ExecutionOrderStatus.WORKING.value:
                            self._cancel(session, deployment, sibling, worker_id, reason="oco_sibling_filled")
            elif state.status == BrokerOrderLookupStatus.WORKING:
                if order.status == ExecutionOrderStatus.UNKNOWN.value:
                    transition_execution_order(
                        session,
                        order.id,
                        ExecutionOrderStatus.WORKING,
                        reconciliation_state=ReconciliationState.RECONCILED,
                        external_order_id=state.external_order_id,
                        producer=worker_id,
                    )
                    self._commit(session)
            elif state.status in (
                BrokerOrderLookupStatus.CANCELLED,
                BrokerOrderLookupStatus.EXPIRED,
                BrokerOrderLookupStatus.REJECTED,
            ):
                target = {
                    BrokerOrderLookupStatus.CANCELLED: ExecutionOrderStatus.CANCELLED,
                    BrokerOrderLookupStatus.EXPIRED: ExecutionOrderStatus.EXPIRED,
                    BrokerOrderLookupStatus.REJECTED: ExecutionOrderStatus.REJECTED,
                }[state.status]
                transition_execution_order(
                    session,
                    order.id,
                    target,
                    rejection_reason=state.message if target == ExecutionOrderStatus.REJECTED else None,
                    completed_at=now,
                    reconciliation_state=(
                        ReconciliationState.RECONCILED if order.status == ExecutionOrderStatus.UNKNOWN.value else None
                    ),
                    producer=worker_id,
                )
                self._commit(session)
            elif (
                state.status == BrokerOrderLookupStatus.NOT_FOUND and order.status == ExecutionOrderStatus.WORKING.value
            ):
                transition_execution_order(
                    session,
                    order.id,
                    ExecutionOrderStatus.UNKNOWN,
                    reconciliation_state=ReconciliationState.PENDING,
                    rejection_reason=state.message,
                    producer=worker_id,
                )
                self._commit(session)

    def _apply_pending_fill(
        self,
        session: Session,
        deployment: Any,
        order: ExecutionOrder,
        state: BrokerOrderState,
        *,
        point_value: Decimal,
        worker_id: str,
    ) -> None:
        self._ledger.apply_fill(
            session,
            paper_account_id=deployment.paper_account_id,
            deployment_id=deployment.id,
            order_id=order.id,
            fill=state.fill,
            point_value=point_value,
            symbol=deployment.symbol,
            reconciling=True,
            producer=worker_id,
            position_opened_at=state.fill.filled_at,
        )
        set_open_position_protection(
            session,
            deployment.id,
            stop_loss=order.stop_loss,
            take_profit=order.take_profit,
            external_position_id=state.external_position_id,
        )
        self._commit(session)

    def _contain_double_fill(
        self,
        session: Session,
        deployment: Any,
        order: ExecutionOrder,
        *,
        intended: ExecutionOrder,
        excess: Decimal,
        excess_side: ExecutionSide,
        cost_config: PaperCostConfig,
        point_value: Decimal,
        worker_id: str,
    ) -> None:
        """Two legs of an OCO group filled: close the excess at market, record it and pause.

        The position left is the one the first leg opened, so it gets that leg's stop and target
        back: on a netting account the second fill has removed them with the position.
        """
        now = self._clock()
        close = create_execution_order_intent(
            session,
            deployment_id=deployment.id,
            decision_id=None,
            broker_mode=BrokerMode(deployment.broker_mode),
            side=excess_side,
            quantity=excess,
            order_type=ExecutionOrderType.MARKET,
            intent_kind=ExecutionIntentKind.CLOSE,
            metadata={"reason": "oco_double_fill", "closes_order_id": str(order.id)},
            intent_committed_at=now,
            producer=worker_id,
        )
        session.flush()
        self._commit(session)
        mark_execution_dispatch_attempt(session, close.id, attempted_at=self._clock(), producer=worker_id)
        self._commit(session)
        broker_mode = BrokerMode(deployment.broker_mode)
        try:
            result = self._broker.submit_market_order(
                MarketOrderRequest(
                    deployment_id=deployment.id,
                    order_id=close.id,
                    symbol=deployment.symbol,
                    side=excess_side,
                    quantity=excess,
                    external_fill_id=f"{broker_mode.value}:{close.id}",
                    broker_mode=broker_mode,
                    intent_created_at=close.intent_committed_at or now,
                    live_activation_enabled=bool(deployment.live_activation_enabled),
                    stop_loss=intended.stop_loss,
                    take_profit=intended.take_profit,
                ),
                cost_config=cost_config,
            )
        except Exception as exc:  # noqa: BLE001 - an ambiguous send must not be retried
            logger.exception("excess close raised for order %s", close.id)
            result = BrokerSubmissionResult(outcome=BrokerSubmissionOutcome.UNKNOWN, metadata={"exception": str(exc)})
        if result.accepted and result.fill is not None:
            self._ledger.apply_fill(
                session,
                paper_account_id=deployment.paper_account_id,
                deployment_id=deployment.id,
                order_id=close.id,
                fill=result.fill,
                point_value=point_value,
                symbol=deployment.symbol,
                producer=worker_id,
            )
            if get_open_net_position(session, deployment.id) is not None:
                set_open_position_protection(
                    session,
                    deployment.id,
                    stop_loss=intended.stop_loss,
                    take_profit=intended.take_profit,
                    external_position_id=result.external_position_id,
                )
        else:
            status = (
                ExecutionOrderStatus.UNKNOWN
                if result.outcome == BrokerSubmissionOutcome.UNKNOWN
                else ExecutionOrderStatus.REJECTED
            )
            transition_execution_order(
                session,
                close.id,
                status,
                rejection_reason=_reason(result, "excess close refused"),
                reconciliation_state=ReconciliationState.PENDING if status == ExecutionOrderStatus.UNKNOWN else None,
                producer=worker_id,
            )
        record_risk_event(
            session,
            deployment_id=deployment.id,
            rejection_code=RiskRejectionCode.OCO_DOUBLE_FILL,
            message="two legs of an OCO group filled before the sibling could be removed",
            order_id=order.id,
            context={"excess_quantity": str(excess), "close_outcome": result.outcome.value},
            producer=worker_id,
        )
        transition_deployment_lifecycle(session, deployment.id, DeploymentLifecycle.PAUSED)
        self._commit(session)
        logger.error("OCO double fill deployment=%s excess=%s; deployment paused", deployment.id, excess)

    def _reconcile_management_intents(self, session: Session, *, deployment: Any, worker_id: str) -> None:
        """Resolve protective and cancellation intents left in doubt by a transport failure."""
        unresolved = session.execute(
            select(ExecutionOrder).where(
                ExecutionOrder.deployment_id == deployment.id,
                ExecutionOrder.intent_kind.in_([ExecutionIntentKind.PROTECT.value, ExecutionIntentKind.CANCEL.value]),
                ExecutionOrder.status == ExecutionOrderStatus.UNKNOWN.value,
            )
        ).scalars()
        for intent in list(unresolved):
            if intent.intent_kind == ExecutionIntentKind.PROTECT.value:
                self._resolve_protection(session, deployment, intent, worker_id=worker_id)
            else:
                self._resolve_cancellation(session, deployment, intent, worker_id=worker_id)

    def _resolve_protection(self, session: Session, deployment: Any, intent: ExecutionOrder, *, worker_id: str) -> None:
        position = get_open_net_position(session, deployment.id)
        if position is None or position.external_position_id is None:
            return
        state = self._broker.lookup_protection(
            ProtectionRequest(
                deployment_id=deployment.id,
                order_id=intent.id,
                symbol=deployment.symbol,
                position_ticket=position.external_position_id,
                stop_loss=intent.stop_loss,
                take_profit=intent.take_profit,
                intent_created_at=intent.intent_committed_at or self._clock(),
                broker_mode=BrokerMode(deployment.broker_mode),
            )
        )
        if state.status == BrokerOrderLookupStatus.FILLED:
            transition_execution_order(
                session,
                intent.id,
                ExecutionOrderStatus.FILLED,
                completed_at=self._clock(),
                reconciliation_state=ReconciliationState.RECONCILED,
                producer=worker_id,
            )
            set_open_position_protection(
                session, deployment.id, stop_loss=intent.stop_loss, take_profit=intent.take_profit
            )
            self._commit(session)
        elif state.status == BrokerOrderLookupStatus.NOT_FOUND:
            transition_execution_order(
                session,
                intent.id,
                ExecutionOrderStatus.REJECTED,
                rejection_reason=state.message,
                completed_at=self._clock(),
                reconciliation_state=ReconciliationState.RECONCILED,
                producer=worker_id,
            )
            self._commit(session)

    def _resolve_cancellation(
        self, session: Session, deployment: Any, intent: ExecutionOrder, *, worker_id: str
    ) -> None:
        target_id = (intent.details or {}).get("target_order_id")
        target = session.get(ExecutionOrder, uuid.UUID(target_id)) if target_id else None
        if target is None or target.external_order_id is None:
            return
        state = self._broker.lookup_cancellation(
            CancelOrderRequest(
                deployment_id=deployment.id,
                order_id=intent.id,
                symbol=deployment.symbol,
                external_order_id=target.external_order_id,
                intent_created_at=intent.intent_committed_at or self._clock(),
                broker_mode=BrokerMode(deployment.broker_mode),
            )
        )
        now = self._clock()
        if state.status == BrokerOrderLookupStatus.CANCELLED:
            transition_execution_order(
                session,
                intent.id,
                ExecutionOrderStatus.CANCELLED,
                completed_at=now,
                reconciliation_state=ReconciliationState.RECONCILED,
                producer=worker_id,
            )
            if target.status in _WORKING_ENTRY:
                transition_execution_order(
                    session,
                    target.id,
                    ExecutionOrderStatus.CANCELLED,
                    completed_at=now,
                    reconciliation_state=(
                        ReconciliationState.RECONCILED if target.status == ExecutionOrderStatus.UNKNOWN.value else None
                    ),
                    producer=worker_id,
                )
            self._commit(session)
        elif state.status == BrokerOrderLookupStatus.NOT_FOUND:
            transition_execution_order(
                session,
                intent.id,
                ExecutionOrderStatus.REJECTED,
                rejection_reason=state.message,
                completed_at=now,
                reconciliation_state=ReconciliationState.RECONCILED,
                producer=worker_id,
            )
            self._commit(session)

    def _reconcile_broker_exit(
        self,
        session: Session,
        *,
        deployment: Any,
        worker_id: str,
        point_value: Decimal,
    ) -> None:
        position = get_open_net_position(session, deployment.id)
        if position is None or not position.is_open or position.external_position_id is None:
            return
        state = self._broker.lookup_position_exit(
            PositionExitRequest(
                deployment_id=deployment.id,
                symbol=deployment.symbol,
                position_ticket=position.external_position_id,
                opened_at=position.opened_at or self._clock(),
                broker_mode=BrokerMode(deployment.broker_mode),
            )
        )
        if state.status != BrokerOrderLookupStatus.FILLED or state.fill is None:
            return

        exit_side = state.fill.side
        now = self._clock()
        close = create_execution_order_intent(
            session,
            deployment_id=deployment.id,
            decision_id=None,
            broker_mode=BrokerMode(deployment.broker_mode),
            side=exit_side,
            quantity=state.fill.quantity,
            order_type=ExecutionOrderType.MARKET,
            intent_kind=ExecutionIntentKind.CLOSE,
            metadata={"exit_reason": state.fill.metadata.get("exit_reason"), "broker_side": True},
            intent_committed_at=now,
            producer=worker_id,
        )
        session.flush()
        self._ledger.apply_fill(
            session,
            paper_account_id=deployment.paper_account_id,
            deployment_id=deployment.id,
            order_id=close.id,
            fill=state.fill,
            point_value=point_value,
            symbol=deployment.symbol,
            producer=worker_id,
        )
        closed = session.get(ExecutionNetPosition, position.id)
        if closed is not None:
            closed.stop_loss = None
            closed.take_profit = None
        self._commit(session)
        logger.info(
            "broker-side exit deployment=%s reason=%s quantity=%s",
            deployment.id,
            state.fill.metadata.get("exit_reason"),
            state.fill.quantity,
        )


def optional_decimal(value: Any) -> Optional[Decimal]:
    if value is None:
        return None
    return Decimal(str(value))


def _float_or_none(value: Optional[Decimal]) -> Optional[float]:
    return float(value) if value is not None else None


def _reason(result: BrokerSubmissionResult, default: str) -> str:
    if result.rejection is not None:
        return result.rejection.message
    return default
