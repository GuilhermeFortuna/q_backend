"""Unknown-order reconciliation: auto (broker) and manual (operator) paths."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from q_backend.execution.brokers.base import (
    BrokerOrderLookupStatus,
    BrokerOrderState,
    MarketOrderRequest,
)
from q_backend.execution.brokers.fakes import (
    FakeQuoteSource,
    FakeReconciliationBroker,
    FixedClock,
    fill_row_count,
)
from q_backend.execution.brokers.paper import PaperBroker
from q_backend.execution.domain import (
    BrokerMode,
    DecisionOutcome,
    DeploymentLifecycle,
    ExecutionOrderStatus,
    ExecutionSide,
    ReconciliationState,
    SignalAction,
)
from q_backend.execution.ledger import ExecutionLedger
from q_backend.execution.reconciliation import OrderReconciler, resolve_order_manually
from q_backend.execution.results import EvaluationPhaseTiming, ForwardDecisionResult
from q_backend.execution.risk import deployment_has_unknown_orders
from q_backend.execution.service import ExecutionService
from q_backend.storage.db.execution_repositories import (
    acquire_worker_lease,
    create_execution_decision,
    create_execution_deployment,
    create_execution_order_intent,
    create_paper_account,
    get_open_net_position,
    get_paper_account,
    list_orders_for_deployment,
    mark_incomplete_orders_unknown,
)
from tests.execution.conftest import strategy_identity


def _clock() -> FixedClock:
    return FixedClock(datetime(2024, 6, 1, 15, 0, tzinfo=timezone.utc))


def _quotes(clock: FixedClock) -> FakeQuoteSource:
    return FakeQuoteSource(
        quotes={"WIN$": (Decimal("130000"), Decimal("130010"))},
        timestamp=clock.now(),
    )


def _buy_result(deployment_id, *, bar_close_time=None) -> ForwardDecisionResult:
    return ForwardDecisionResult(
        deployment_id=str(deployment_id),
        bar_close_time=bar_close_time or datetime(2024, 6, 1, 14, 0, tzinfo=timezone.utc),
        bar_close_price=130005.0,
        signal_action=SignalAction.BUY,
        reason="test buy",
        requested_quantity=1.0,
        strategy_name="MACrossover",
        strategy_version=1,
        config_hash="test-hash",
        symbol="WIN$",
        timeframe="H1",
        timing=EvaluationPhaseTiming(total_ms=1.0),
    )


def _seed_running_deployment(session, name: str):
    account = create_paper_account(session, name=f"{name}-acct", initial_balance=Decimal("100000"))
    deployment = create_execution_deployment(
        session,
        paper_account_id=account.id,
        name=name,
        identity=strategy_identity(),
        lifecycle=DeploymentLifecycle.RUNNING,
    )
    acquire_worker_lease(
        session,
        deployment_id=deployment.id,
        worker_id="worker-a",
        lease_token=f"{name}-token",
        ttl_seconds=60,
        now=datetime(2024, 6, 1, 15, 0, tzinfo=timezone.utc),
    )
    session.flush()
    return account, deployment


def _pending_unknown_order(session, deployment, *, side=ExecutionSide.BUY):
    decision = create_execution_decision(
        session,
        deployment_id=deployment.id,
        bar_close_time=datetime(2024, 6, 1, 14, 0, tzinfo=timezone.utc),
        identity=strategy_identity(),
        signal_action=SignalAction.BUY,
        outcome=DecisionOutcome.SIGNAL,
        requested_quantity=Decimal("1"),
    )
    order = create_execution_order_intent(
        session,
        deployment_id=deployment.id,
        decision_id=decision.id,
        broker_mode=BrokerMode.PAPER,
        side=side,
        quantity=Decimal("1"),
    )
    mark_incomplete_orders_unknown(session, deployment.id)
    session.flush()
    session.refresh(order)
    assert order.status == ExecutionOrderStatus.UNKNOWN.value
    assert order.reconciliation_state == ReconciliationState.PENDING.value
    return decision, order


def test_reconcile_filled_matches_never_crashed_run(db_session, paper_cost_config):
    clock = _clock()
    quotes = _quotes(clock)

    # A: happy path (never crashed) via the normal service.
    _, dep_a = _seed_running_deployment(db_session, "happy")
    service = ExecutionService(
        broker=PaperBroker(quote_source=quotes, clock=clock),
        quote_source=quotes,
        ledger=ExecutionLedger(),
        max_bar_age_seconds=7200.0,
        clock=clock.now,
    )
    service.process_completed_bar(
        db_session,
        deployment=dep_a,
        eval_result=_buy_result(dep_a.id),
        lease_token="happy-token",
        worker_id="worker-a",
        cost_config=paper_cost_config,
        point_value=Decimal("0.2"),
    )
    pos_a = get_open_net_position(db_session, dep_a.id)
    cash_a = get_paper_account(db_session, dep_a.paper_account_id).cash_balance

    # B: crashed into UNKNOWN, then reconciled from a broker-confirmed fill.
    acct_b, dep_b = _seed_running_deployment(db_session, "crashed")
    _, order_b = _pending_unknown_order(db_session, dep_b)

    paper = PaperBroker(quote_source=quotes, clock=clock)
    confirmed = paper.submit_market_order(
        MarketOrderRequest(
            deployment_id=dep_b.id,
            order_id=order_b.id,
            symbol=dep_b.symbol,
            side=ExecutionSide(order_b.side),
            quantity=order_b.quantity,
            external_fill_id=f"paper:{order_b.id}",
        ),
        cost_config=paper_cost_config,
    )
    assert confirmed.fill is not None

    broker = FakeReconciliationBroker()
    broker.set_state(
        order_b.id,
        BrokerOrderState(status=BrokerOrderLookupStatus.FILLED, fill=confirmed.fill),
    )
    reconciler = OrderReconciler(
        broker=broker,
        ledger=ExecutionLedger(),
        point_value=Decimal("0.2"),
        clock=clock.now,
    )
    outcomes = reconciler.reconcile_deployment(db_session, dep_b)
    db_session.flush()
    db_session.refresh(order_b)

    assert len(outcomes) == 1
    assert order_b.status == ExecutionOrderStatus.FILLED.value
    assert order_b.reconciliation_state == ReconciliationState.RECONCILED.value
    assert order_b.reconciled_by == "reconciler"
    assert order_b.reconciled_at is not None

    pos_b = get_open_net_position(db_session, dep_b.id)
    cash_b = get_paper_account(db_session, acct_b.id).cash_balance
    assert pos_b is not None and pos_b.is_open
    assert (pos_b.side, pos_b.quantity, pos_b.average_entry_price) == (
        pos_a.side,
        pos_a.quantity,
        pos_a.average_entry_price,
    )
    assert cash_b == cash_a
    assert not deployment_has_unknown_orders(list_orders_for_deployment(db_session, dep_b.id))


def test_reconcile_not_found_fails_order_and_unblocks(db_session):
    _, deployment = _seed_running_deployment(db_session, "notfound")
    _, order = _pending_unknown_order(db_session, deployment)
    cash_before = get_paper_account(db_session, deployment.paper_account_id).cash_balance

    broker = FakeReconciliationBroker(default=BrokerOrderState(status=BrokerOrderLookupStatus.NOT_FOUND))
    reconciler = OrderReconciler(broker=broker, ledger=ExecutionLedger(), point_value=Decimal("0.2"))
    reconciler.reconcile_deployment(db_session, deployment)
    db_session.flush()
    db_session.refresh(order)

    assert order.status == ExecutionOrderStatus.REJECTED.value
    assert order.reconciliation_state == ReconciliationState.RECONCILED.value
    assert fill_row_count(db_session) == 0
    cash_after = get_paper_account(db_session, deployment.paper_account_id).cash_balance
    assert cash_after == cash_before
    assert not deployment_has_unknown_orders(list_orders_for_deployment(db_session, deployment.id))


def test_reconcile_unavailable_stays_pending_and_blocks(db_session, paper_cost_config):
    clock = _clock()
    quotes = _quotes(clock)
    _, deployment = _seed_running_deployment(db_session, "unavail")
    _, order = _pending_unknown_order(db_session, deployment)

    broker = FakeReconciliationBroker(
        default=BrokerOrderState(status=BrokerOrderLookupStatus.UNAVAILABLE, message="broker offline")
    )
    reconciler = OrderReconciler(broker=broker, ledger=ExecutionLedger(), point_value=Decimal("0.2"), clock=clock.now)
    reconciler.reconcile_deployment(db_session, deployment)
    db_session.flush()
    db_session.refresh(order)

    assert order.status == ExecutionOrderStatus.UNKNOWN.value
    assert order.reconciliation_state == ReconciliationState.PENDING.value
    assert order.reconciliation_attempted_at is not None
    assert order.reconciliation_error == "broker offline"
    assert deployment_has_unknown_orders(list_orders_for_deployment(db_session, deployment.id))

    # A new submission is blocked with the structured risk reason.
    service = ExecutionService(
        broker=PaperBroker(quote_source=quotes, clock=clock),
        quote_source=quotes,
        ledger=ExecutionLedger(),
        max_bar_age_seconds=7200.0,
        clock=clock.now,
    )
    result = service.process_completed_bar(
        db_session,
        deployment=deployment,
        eval_result=_buy_result(
            deployment.id,
            bar_close_time=datetime(2024, 6, 1, 15, 0, tzinfo=timezone.utc),
        ),
        lease_token="unavail-token",
        worker_id="worker-a",
        cost_config=paper_cost_config,
        point_value=Decimal("0.2"),
    )
    assert result.outcome == DecisionOutcome.RISK_REJECTED
    assert result.rejection is not None
    assert result.rejection.code.value == "unknown_prior_order"


def test_manual_resolution_filled_and_not_filled(db_session):
    ledger = ExecutionLedger()

    # not_filled: order fails, ledger untouched.
    _, dep_nf = _seed_running_deployment(db_session, "manual-nf")
    _, order_nf = _pending_unknown_order(db_session, dep_nf)
    outcome_nf = resolve_order_manually(
        db_session,
        order=order_nf,
        deployment=dep_nf,
        outcome="not_filled",
        actor="operator-1",
        note="broker confirmed no fill by phone",
        ledger=ledger,
        point_value=Decimal("0.2"),
    )
    db_session.flush()
    db_session.refresh(order_nf)
    assert outcome_nf.resolution.value == "failed"
    assert order_nf.status == ExecutionOrderStatus.REJECTED.value
    assert order_nf.reconciled_by == "operator-1"
    assert "phone" in (order_nf.reconciliation_detail or "")
    assert fill_row_count(db_session) == 0

    # Re-resolving a resolved order is rejected.
    from q_backend.execution.reconciliation import OrderNotPendingError

    with pytest.raises(OrderNotPendingError):
        resolve_order_manually(
            db_session,
            order=order_nf,
            deployment=dep_nf,
            outcome="not_filled",
            actor="operator-1",
            note="again",
            ledger=ledger,
            point_value=Decimal("0.2"),
        )
