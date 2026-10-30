"""Working stop and limit entries, protective levels and broker-side exits, against a scripted broker."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Optional

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from q_backend.execution.brokers.base import (
    BrokerOrderLookupStatus,
    BrokerOrderState,
    BrokerRejection,
    BrokerRejectionCode,
    BrokerSubmissionOutcome,
    BrokerSubmissionResult,
)
from q_backend.execution.domain import (
    ExecutionIntentKind,
    ExecutionOrderStatus,
    ExecutionOrderType,
    ExecutionSide,
    FillRecord,
    BrokerMode,
    PositionSide,
    ReconciliationState,
    RiskRejectionCode,
)
from q_backend.execution.ledger import ExecutionLedger
from q_backend.execution.protective import ProtectiveOrders
from q_backend.execution.results import EvaluationPhaseTiming, ForwardDecisionResult
from q_backend.storage.db.execution_models import ExecutionNetPosition, ExecutionOrder, ExecutionRiskEvent
from q_backend.storage.db.execution_repositories import (
    create_execution_order_intent,
    get_open_net_position,
    list_working_orders,
    transition_execution_order,
)

NOW = datetime(2024, 6, 1, 15, 0, tzinfo=timezone.utc)
EXPIRY = NOW + timedelta(hours=2)


class ScriptedBroker:
    """Records every call and answers from queues; unscripted calls get a sensible default."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.pending_results: list[BrokerSubmissionResult] = []
        self.cancel_results: list[BrokerSubmissionResult] = []
        self.protection_results: list[BrokerSubmissionResult] = []
        self.pending_states: list[BrokerOrderState] = []
        self.protection_states: list[BrokerOrderState] = []
        self.cancellation_states: list[BrokerOrderState] = []
        self.exit_state: BrokerOrderState = BrokerOrderState(status=BrokerOrderLookupStatus.NOT_FOUND)
        self._ticket = 500

    def health(self):  # pragma: no cover - not used by the manager
        raise NotImplementedError

    def submit_market_order(self, request, *, cost_config):  # pragma: no cover
        raise NotImplementedError

    def lookup_order(self, request):  # pragma: no cover
        raise NotImplementedError

    def submit_pending_order(self, request):
        self.calls.append(("submit_pending", request))
        if self.pending_results:
            return self.pending_results.pop(0)
        self._ticket += 1
        return BrokerSubmissionResult(outcome=BrokerSubmissionOutcome.WORKING, external_order_id=str(self._ticket))

    def lookup_pending_order(self, request):
        self.calls.append(("lookup_pending", request.order_id))
        if self.pending_states:
            return self.pending_states.pop(0)
        return BrokerOrderState(status=BrokerOrderLookupStatus.WORKING, external_order_id=None)

    def set_protection(self, request):
        self.calls.append(("set_protection", request))
        if self.protection_results:
            return self.protection_results.pop(0)
        return BrokerSubmissionResult(
            outcome=BrokerSubmissionOutcome.APPLIED, external_position_id=request.position_ticket
        )

    def cancel_order(self, request):
        self.calls.append(("cancel", request))
        if self.cancel_results:
            return self.cancel_results.pop(0)
        return BrokerSubmissionResult(outcome=BrokerSubmissionOutcome.CANCELLED)

    def lookup_protection(self, request):
        self.calls.append(("lookup_protection", request.order_id))
        return self.protection_states.pop(0)

    def lookup_cancellation(self, request):
        self.calls.append(("lookup_cancellation", request.order_id))
        return self.cancellation_states.pop(0)

    def lookup_position_exit(self, request):
        self.calls.append(("lookup_exit", request.position_ticket))
        return self.exit_state

    def named(self, name: str) -> list[Any]:
        return [request for kind, request in self.calls if kind == name]


def _risk_allow(*args, **kwargs) -> Optional[Any]:
    return None


def _risk_block(*args, **kwargs):
    from q_backend.execution.domain import RiskRejection

    return RiskRejection(code=RiskRejectionCode.NOTIONAL_LIMIT, message="over the notional limit")


def _manager(broker: ScriptedBroker, *, risk=_risk_allow) -> ProtectiveOrders:
    return ProtectiveOrders(
        broker=broker,
        ledger=ExecutionLedger(),
        clock=lambda: NOW,
        commit=lambda session: session.flush(),
        risk_check=risk,
    )


def _spec(**overrides) -> dict[str, Any]:
    spec = {
        "side": "buy",
        "order_type": "stop",
        "price": 130100.0,
        "stop_loss": 129900.0,
        "take_profit": 130400.0,
        "expires_at": EXPIRY,
        "reason": "breakout",
    }
    spec.update(overrides)
    return spec


def _forward(**overrides) -> ForwardDecisionResult:
    values = dict(
        deployment_id="dep",
        bar_close_time=NOW,
        bar_close_price=130000.0,
        signal_action="hold",
        strategy_name="test",
        strategy_version=1,
        config_hash="hash",
        symbol="WIN$",
        timeframe="H1",
        timing=EvaluationPhaseTiming(),
        requested_quantity=1.0,
    )
    values.update(overrides)
    return ForwardDecisionResult(**values)


def _deployment(seeded):
    _account, deployment, token = seeded
    return deployment, token


def _open_long(session: Session, deployment, *, external_position_id: str = "42") -> ExecutionNetPosition:
    order = create_execution_order_intent(
        session,
        deployment_id=deployment.id,
        decision_id=None,
        broker_mode=BrokerMode.PAPER,
        side=ExecutionSide.BUY,
        quantity=Decimal("1"),
    )
    ExecutionLedger().apply_fill(
        session,
        paper_account_id=deployment.paper_account_id,
        deployment_id=deployment.id,
        order_id=order.id,
        fill=FillRecord(
            broker_mode=BrokerMode.MT5_LIVE,
            external_fill_id=f"seed-{order.id}",
            side=ExecutionSide.BUY,
            quantity=Decimal("1"),
            price=Decimal("130000"),
            filled_at=NOW,
            metadata={},
        ),
        point_value=Decimal("0.2"),
        symbol="WIN$",
        position_opened_at=NOW,
    )
    position = get_open_net_position(session, deployment.id)
    position.external_position_id = external_position_id
    position.stop_loss = Decimal("129900")
    position.take_profit = Decimal("130400")
    session.flush()
    return position


def _working(session: Session, deployment) -> list[ExecutionOrder]:
    return list_working_orders(session, deployment.id)


def test_place_pending_records_a_working_stop_entry_with_its_levels_and_expiry(db_session, seeded_deployment):
    deployment, token = _deployment(seeded_deployment)
    broker = ScriptedBroker()

    outcome = _manager(broker).place_pending(
        db_session,
        deployment=deployment,
        spec=_spec(),
        quantity=Decimal("1"),
        bar_close_time=NOW,
        decision_id=None,
        lease_token=token,
        worker_id="worker-a",
        cost_config=None,
        point_value=Decimal("0.2"),
    )

    assert outcome == "working"
    [order] = _working(db_session, deployment)
    assert order.status == ExecutionOrderStatus.WORKING.value
    assert order.order_type == ExecutionOrderType.STOP.value
    assert order.intent_kind == ExecutionIntentKind.ENTRY.value
    assert order.price == Decimal("130100")
    assert order.stop_loss == Decimal("129900")
    assert order.take_profit == Decimal("130400")
    assert order.expires_at.replace(tzinfo=timezone.utc) == EXPIRY
    assert order.external_order_id is not None
    [request] = broker.named("submit_pending")
    assert request.expires_at == EXPIRY and request.side == ExecutionSide.BUY


def test_a_new_resting_entry_removes_the_working_one_before_placing(db_session, seeded_deployment):
    deployment, token = _deployment(seeded_deployment)
    broker = ScriptedBroker()
    manager = _manager(broker)
    kwargs = dict(
        deployment=deployment,
        quantity=Decimal("1"),
        bar_close_time=NOW,
        decision_id=None,
        lease_token=token,
        worker_id="worker-a",
        cost_config=None,
        point_value=Decimal("0.2"),
    )
    manager.place_pending(db_session, spec=_spec(), **kwargs)
    first = _working(db_session, deployment)[0]

    outcome = manager.place_pending(db_session, spec=_spec(price=130200.0), **kwargs)

    assert outcome == "working"
    [cancel] = broker.named("cancel")
    assert cancel.external_order_id == first.external_order_id
    db_session.refresh(first)
    assert first.status == ExecutionOrderStatus.CANCELLED.value
    [working] = _working(db_session, deployment)
    assert working.id != first.id
    assert working.price == Decimal("130200")


def test_a_refused_removal_blocks_the_replacement(db_session, seeded_deployment):
    deployment, token = _deployment(seeded_deployment)
    broker = ScriptedBroker()
    manager = _manager(broker)
    kwargs = dict(
        deployment=deployment,
        quantity=Decimal("1"),
        bar_close_time=NOW,
        decision_id=None,
        lease_token=token,
        worker_id="worker-a",
        cost_config=None,
        point_value=Decimal("0.2"),
    )
    manager.place_pending(db_session, spec=_spec(), **kwargs)
    broker.cancel_results = [
        BrokerSubmissionResult(
            outcome=BrokerSubmissionOutcome.REJECTED,
            rejection=BrokerRejection(code=BrokerRejectionCode.ORDER_SEND_FAILED, message="order already filled"),
        )
    ]
    submits_before = len(broker.named("submit_pending"))

    outcome = manager.place_pending(db_session, spec=_spec(price=130200.0), **kwargs)

    assert outcome == "replace_rejected"
    assert len(broker.named("submit_pending")) == submits_before
    [still] = _working(db_session, deployment)
    assert still.status == ExecutionOrderStatus.WORKING.value


def test_risk_rejection_places_nothing(db_session, seeded_deployment):
    deployment, token = _deployment(seeded_deployment)
    broker = ScriptedBroker()

    outcome = _manager(broker, risk=_risk_block).place_pending(
        db_session,
        deployment=deployment,
        spec=_spec(),
        quantity=Decimal("1"),
        bar_close_time=NOW,
        decision_id=None,
        lease_token=token,
        worker_id="worker-a",
        cost_config=None,
        point_value=Decimal("0.2"),
    )

    assert outcome == "risk_rejected"
    assert broker.named("submit_pending") == []


def test_no_pending_entry_is_placed_while_a_position_is_open(db_session, seeded_deployment):
    deployment, token = _deployment(seeded_deployment)
    _open_long(db_session, deployment)
    broker = ScriptedBroker()

    outcome = _manager(broker).place_pending(
        db_session,
        deployment=deployment,
        spec=_spec(),
        quantity=Decimal("1"),
        bar_close_time=NOW,
        decision_id=None,
        lease_token=token,
        worker_id="worker-a",
        cost_config=None,
        point_value=Decimal("0.2"),
    )

    assert outcome == "position_open"
    assert broker.named("submit_pending") == []


def test_a_broker_rejection_of_the_pending_order_is_recorded(db_session, seeded_deployment):
    deployment, token = _deployment(seeded_deployment)
    broker = ScriptedBroker()
    broker.pending_results = [
        BrokerSubmissionResult(
            outcome=BrokerSubmissionOutcome.REJECTED,
            rejection=BrokerRejection(code=BrokerRejectionCode.ORDER_SEND_FAILED, message="invalid stops"),
        )
    ]

    outcome = _manager(broker).place_pending(
        db_session,
        deployment=deployment,
        spec=_spec(),
        quantity=Decimal("1"),
        bar_close_time=NOW,
        decision_id=None,
        lease_token=token,
        worker_id="worker-a",
        cost_config=None,
        point_value=Decimal("0.2"),
    )

    assert outcome == "rejected"
    [order] = db_session.execute(select(ExecutionOrder).where(ExecutionOrder.deployment_id == deployment.id)).scalars()
    assert order.status == ExecutionOrderStatus.REJECTED.value
    assert order.rejection_reason == "invalid stops"


def test_reconciliation_fills_the_entry_into_a_protected_position(db_session, seeded_deployment):
    deployment, token = _deployment(seeded_deployment)
    broker = ScriptedBroker()
    manager = _manager(broker)
    manager.place_pending(
        db_session,
        deployment=deployment,
        spec=_spec(),
        quantity=Decimal("1"),
        bar_close_time=NOW,
        decision_id=None,
        lease_token=token,
        worker_id="worker-a",
        cost_config=None,
        point_value=Decimal("0.2"),
    )
    fill = FillRecord(
        broker_mode=BrokerMode.MT5_LIVE,
        external_fill_id="5001",
        side=ExecutionSide.BUY,
        quantity=Decimal("1"),
        price=Decimal("130105"),
        filled_at=NOW + timedelta(minutes=20),
        metadata={},
    )
    broker.pending_states = [
        BrokerOrderState(status=BrokerOrderLookupStatus.FILLED, fill=fill, external_position_id="42")
    ]

    manager.reconcile(db_session, deployment=deployment, worker_id="worker-a", point_value=Decimal("0.2"))

    assert _working(db_session, deployment) == []
    position = get_open_net_position(db_session, deployment.id)
    assert position is not None
    assert PositionSide(position.side) == PositionSide.LONG
    assert position.stop_loss == Decimal("129900")
    assert position.take_profit == Decimal("130400")
    assert position.external_position_id == "42"


def test_reconciliation_records_expiry_and_cancellation_from_the_broker(db_session, seeded_deployment):
    deployment, token = _deployment(seeded_deployment)
    broker = ScriptedBroker()
    manager = _manager(broker)
    kwargs = dict(
        deployment=deployment,
        quantity=Decimal("1"),
        bar_close_time=NOW,
        decision_id=None,
        lease_token=token,
        worker_id="worker-a",
        cost_config=None,
        point_value=Decimal("0.2"),
    )
    manager.place_pending(db_session, spec=_spec(), **kwargs)
    broker.pending_states = [BrokerOrderState(status=BrokerOrderLookupStatus.EXPIRED)]

    manager.reconcile(db_session, deployment=deployment, worker_id="worker-a", point_value=Decimal("0.2"))

    [order] = db_session.execute(select(ExecutionOrder).where(ExecutionOrder.deployment_id == deployment.id)).scalars()
    assert order.status == ExecutionOrderStatus.EXPIRED.value
    assert _working(db_session, deployment) == []


def test_an_entry_the_broker_cannot_find_is_held_for_reconciliation(db_session, seeded_deployment):
    deployment, token = _deployment(seeded_deployment)
    broker = ScriptedBroker()
    manager = _manager(broker)
    manager.place_pending(
        db_session,
        deployment=deployment,
        spec=_spec(),
        quantity=Decimal("1"),
        bar_close_time=NOW,
        decision_id=None,
        lease_token=token,
        worker_id="worker-a",
        cost_config=None,
        point_value=Decimal("0.2"),
    )
    broker.pending_states = [BrokerOrderState(status=BrokerOrderLookupStatus.NOT_FOUND, message="gone")]
    manager.reconcile(db_session, deployment=deployment, worker_id="worker-a", point_value=Decimal("0.2"))
    [order] = db_session.execute(select(ExecutionOrder).where(ExecutionOrder.deployment_id == deployment.id)).scalars()
    assert order.status == ExecutionOrderStatus.UNKNOWN.value
    assert order.reconciliation_state == ReconciliationState.PENDING.value

    broker.pending_states = [BrokerOrderState(status=BrokerOrderLookupStatus.WORKING, external_order_id="501")]
    manager.reconcile(db_session, deployment=deployment, worker_id="worker-a", point_value=Decimal("0.2"))
    db_session.refresh(order)
    assert order.status == ExecutionOrderStatus.WORKING.value
    assert order.reconciliation_state == ReconciliationState.RECONCILED.value


def test_amend_moves_the_stop_and_keeps_the_target(db_session, seeded_deployment):
    deployment, token = _deployment(seeded_deployment)
    position = _open_long(db_session, deployment)
    broker = ScriptedBroker()

    outcome = _manager(broker).amend_position(
        db_session,
        deployment=deployment,
        levels={"stop_loss": 129950.0, "take_profit": None},
        decision_id=None,
        worker_id="worker-a",
    )

    assert outcome == "applied"
    [request] = broker.named("set_protection")
    assert request.stop_loss == Decimal("129950")
    assert request.take_profit == Decimal("130400")
    db_session.refresh(position)
    assert position.stop_loss == Decimal("129950")
    assert position.take_profit == Decimal("130400")
    protect = db_session.execute(
        select(ExecutionOrder).where(ExecutionOrder.intent_kind == ExecutionIntentKind.PROTECT.value)
    ).scalar_one()
    assert protect.status == ExecutionOrderStatus.FILLED.value


def test_a_rejected_amendment_keeps_the_levels_and_records_a_risk_event(db_session, seeded_deployment):
    deployment, token = _deployment(seeded_deployment)
    position = _open_long(db_session, deployment)
    broker = ScriptedBroker()
    broker.protection_results = [
        BrokerSubmissionResult(
            outcome=BrokerSubmissionOutcome.REJECTED,
            rejection=BrokerRejection(code=BrokerRejectionCode.PROTECTIVE_LEVEL_REJECTED, message="stop too close"),
        )
    ]

    outcome = _manager(broker).amend_position(
        db_session,
        deployment=deployment,
        levels={"stop_loss": 130090.0},
        decision_id=None,
        worker_id="worker-a",
    )

    assert outcome == "rejected"
    db_session.refresh(position)
    assert position.stop_loss == Decimal("129900")
    event = db_session.execute(select(ExecutionRiskEvent)).scalar_one()
    assert event.rejection_code == RiskRejectionCode.PROTECTIVE_LEVEL_REJECTED.value
    assert event.message == "stop too close"


def test_an_unknown_amendment_is_resolved_by_reading_the_levels(db_session, seeded_deployment):
    deployment, token = _deployment(seeded_deployment)
    position = _open_long(db_session, deployment)
    broker = ScriptedBroker()
    broker.protection_results = [BrokerSubmissionResult(outcome=BrokerSubmissionOutcome.UNKNOWN)]
    manager = _manager(broker)

    assert (
        manager.amend_position(
            db_session,
            deployment=deployment,
            levels={"stop_loss": 129950.0},
            decision_id=None,
            worker_id="worker-a",
        )
        == "unknown"
    )
    protect = db_session.execute(
        select(ExecutionOrder).where(ExecutionOrder.intent_kind == ExecutionIntentKind.PROTECT.value)
    ).scalar_one()
    assert protect.status == ExecutionOrderStatus.UNKNOWN.value

    broker.protection_states = [BrokerOrderState(status=BrokerOrderLookupStatus.FILLED)]
    manager.reconcile(db_session, deployment=deployment, worker_id="worker-a", point_value=Decimal("0.2"))
    db_session.refresh(protect)
    assert protect.status == ExecutionOrderStatus.FILLED.value
    db_session.refresh(position)
    assert position.stop_loss == Decimal("129950")


def test_a_broker_side_stop_exit_closes_the_position_and_records_its_reason(db_session, seeded_deployment):
    deployment, token = _deployment(seeded_deployment)
    position = _open_long(db_session, deployment)
    broker = ScriptedBroker()
    broker.exit_state = BrokerOrderState(
        status=BrokerOrderLookupStatus.FILLED,
        fill=FillRecord(
            broker_mode=BrokerMode.MT5_LIVE,
            external_fill_id="6001",
            side=ExecutionSide.SELL,
            quantity=Decimal("1"),
            price=Decimal("129900"),
            filled_at=NOW + timedelta(minutes=45),
            metadata={"exit_reason": "stop_loss"},
        ),
        external_position_id="42",
    )

    _manager(broker).reconcile(db_session, deployment=deployment, worker_id="worker-a", point_value=Decimal("0.2"))

    assert get_open_net_position(db_session, deployment.id) is None
    close = db_session.execute(
        select(ExecutionOrder).where(ExecutionOrder.intent_kind == ExecutionIntentKind.CLOSE.value)
    ).scalar_one()
    assert close.status == ExecutionOrderStatus.FILLED.value
    assert close.details["exit_reason"] == "stop_loss"
    db_session.refresh(position)
    assert position.is_open is False


def test_a_still_open_position_has_no_broker_exit(db_session, seeded_deployment):
    deployment, token = _deployment(seeded_deployment)
    _open_long(db_session, deployment)
    broker = ScriptedBroker()

    _manager(broker).reconcile(db_session, deployment=deployment, worker_id="worker-a", point_value=Decimal("0.2"))

    assert get_open_net_position(db_session, deployment.id) is not None
    assert (
        db_session.execute(
            select(ExecutionOrder).where(ExecutionOrder.intent_kind == ExecutionIntentKind.CLOSE.value)
        ).first()
        is None
    )


def test_remove_working_cancels_every_resting_entry(db_session, seeded_deployment):
    deployment, token = _deployment(seeded_deployment)
    broker = ScriptedBroker()
    manager = _manager(broker)
    manager.place_pending(
        db_session,
        deployment=deployment,
        spec=_spec(),
        quantity=Decimal("1"),
        bar_close_time=NOW,
        decision_id=None,
        lease_token=token,
        worker_id="worker-a",
        cost_config=None,
        point_value=Decimal("0.2"),
    )

    assert manager.remove_working(db_session, deployment=deployment, worker_id="worker-a", reason="pause") is True
    assert _working(db_session, deployment) == []
    [cancel] = broker.named("cancel")
    assert cancel.external_order_id is not None


def test_a_cancellation_left_in_doubt_is_resolved_against_the_resting_order(db_session, seeded_deployment):
    deployment, token = _deployment(seeded_deployment)
    broker = ScriptedBroker()
    manager = _manager(broker)
    manager.place_pending(
        db_session,
        deployment=deployment,
        spec=_spec(),
        quantity=Decimal("1"),
        bar_close_time=NOW,
        decision_id=None,
        lease_token=token,
        worker_id="worker-a",
        cost_config=None,
        point_value=Decimal("0.2"),
    )
    broker.cancel_results = [BrokerSubmissionResult(outcome=BrokerSubmissionOutcome.UNKNOWN)]
    assert manager.remove_working(db_session, deployment=deployment, worker_id="worker-a", reason="pause") is False
    cancel = db_session.execute(
        select(ExecutionOrder).where(ExecutionOrder.intent_kind == ExecutionIntentKind.CANCEL.value)
    ).scalar_one()
    assert cancel.status == ExecutionOrderStatus.UNKNOWN.value

    broker.cancellation_states = [BrokerOrderState(status=BrokerOrderLookupStatus.CANCELLED)]
    manager.reconcile(db_session, deployment=deployment, worker_id="worker-a", point_value=Decimal("0.2"))

    db_session.refresh(cancel)
    assert cancel.status == ExecutionOrderStatus.CANCELLED.value
    target = db_session.execute(
        select(ExecutionOrder).where(ExecutionOrder.intent_kind == ExecutionIntentKind.ENTRY.value)
    ).scalar_one()
    assert target.status == ExecutionOrderStatus.CANCELLED.value


def test_apply_bar_cancels_then_places_and_amends_in_order(db_session, seeded_deployment):
    deployment, token = _deployment(seeded_deployment)
    broker = ScriptedBroker()
    manager = _manager(broker)
    manager.place_pending(
        db_session,
        deployment=deployment,
        spec=_spec(),
        quantity=Decimal("1"),
        bar_close_time=NOW,
        decision_id=None,
        lease_token=token,
        worker_id="worker-a",
        cost_config=None,
        point_value=Decimal("0.2"),
    )
    manager.apply_bar(
        db_session,
        deployment=deployment,
        eval_result=_forward(cancel_pending=True, pending_entry=_spec(price=130250.0)),
        decision_id=None,
        lease_token=token,
        worker_id="worker-a",
        cost_config=None,
        point_value=Decimal("0.2"),
    )
    kinds = [kind for kind, _ in broker.calls]
    last_submit = max(index for index, kind in enumerate(kinds) if kind == "submit_pending")
    assert kinds.index("cancel") < last_submit
    [working] = _working(db_session, deployment)
    assert working.price == Decimal("130250")


@pytest.mark.parametrize("reason", ["pause", "kill_switch"])
def test_working_orders_are_removed_for_non_trading_deployments(db_session, seeded_deployment, reason):
    deployment, token = _deployment(seeded_deployment)
    broker = ScriptedBroker()
    manager = _manager(broker)
    manager.place_pending(
        db_session,
        deployment=deployment,
        spec=_spec(),
        quantity=Decimal("1"),
        bar_close_time=NOW,
        decision_id=None,
        lease_token=token,
        worker_id="worker-a",
        cost_config=None,
        point_value=Decimal("0.2"),
    )
    assert manager.remove_working(db_session, deployment=deployment, worker_id="worker-a", reason=reason)
    assert _working(db_session, deployment) == []
