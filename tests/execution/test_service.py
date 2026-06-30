"""ExecutionService end-to-end, crash injection, and deduplication tests."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from q_backend.execution.brokers.base import PaperCostConfig
from q_backend.execution.brokers.fakes import FakeQuoteSource, FixedClock, fill_row_count
from q_backend.execution.brokers.paper import PaperBroker
from q_backend.execution.domain import (
    DecisionOutcome,
    DeploymentLifecycle,
    ExecutionOrderStatus,
    SignalAction,
)
from q_backend.execution.ledger import ExecutionLedger
from q_backend.execution.results import EvaluationPhaseTiming, ForwardDecisionResult
from q_backend.execution.service import CrashInjector, ExecutionService
from q_backend.storage.db.execution_models import ExecutionDecision, ExecutionOrder
from q_backend.storage.db.execution_repositories import (
    acquire_worker_lease,
    create_execution_deployment,
    create_execution_order_intent,
    create_paper_account,
    get_open_net_position,
    list_orders_for_deployment,
    mark_incomplete_orders_unknown,
    set_kill_switch,
    transition_deployment_lifecycle,
)
from tests.execution.conftest import strategy_identity


def _buy_result(
    deployment_id,
    *,
    bar_close_time: datetime | None = None,
) -> ForwardDecisionResult:
    return ForwardDecisionResult(
        deployment_id=str(deployment_id),
        bar_close_time=bar_close_time
        or datetime(2024, 6, 1, 14, 0, tzinfo=timezone.utc),
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


def _service(
    clock: FixedClock,
    quotes: FakeQuoteSource,
    *,
    crash: CrashInjector | None = None,
    commit=None,
) -> ExecutionService:
    broker = PaperBroker(quote_source=quotes, clock=clock)
    return ExecutionService(
        broker=broker,
        quote_source=quotes,
        ledger=ExecutionLedger(),
        crash_injector=crash,
        commit=commit,
        max_bar_age_seconds=7200.0,
        clock=clock.now,
    )


def test_e2e_completed_bar_produces_one_durable_fill(
    db_session, seeded_deployment, paper_cost_config
):
    _, deployment, token = seeded_deployment
    clock = FixedClock(datetime(2024, 6, 1, 15, 0, tzinfo=timezone.utc))
    quotes = FakeQuoteSource(
        quotes={"WIN$": (Decimal("130000"), Decimal("130010"))},
        timestamp=clock.now(),
    )
    service = _service(clock, quotes)
    result = service.process_completed_bar(
        db_session,
        deployment=deployment,
        eval_result=_buy_result(deployment.id),
        lease_token=token,
        worker_id="worker-a",
        cost_config=paper_cost_config,
        point_value=Decimal("0.2"),
    )
    assert result.outcome == DecisionOutcome.ORDER_FILLED
    assert result.duplicate is False
    assert fill_row_count(db_session) == 1
    position = get_open_net_position(db_session, deployment.id)
    assert position is not None
    assert position.is_open


def test_duplicate_bar_processing_is_idempotent(db_session, seeded_deployment, paper_cost_config):
    _, deployment, token = seeded_deployment
    clock = FixedClock(datetime(2024, 6, 1, 15, 0, tzinfo=timezone.utc))
    quotes = FakeQuoteSource(
        quotes={"WIN$": (Decimal("130000"), Decimal("130010"))},
        timestamp=clock.now(),
    )
    service = _service(clock, quotes)
    eval_result = _buy_result(deployment.id)
    first = service.process_completed_bar(
        db_session,
        deployment=deployment,
        eval_result=eval_result,
        lease_token=token,
        worker_id="worker-a",
        cost_config=paper_cost_config,
        point_value=Decimal("0.2"),
    )
    second = service.process_completed_bar(
        db_session,
        deployment=deployment,
        eval_result=eval_result,
        lease_token=token,
        worker_id="worker-a",
        cost_config=paper_cost_config,
        point_value=Decimal("0.2"),
    )
    assert first.outcome == DecisionOutcome.ORDER_FILLED
    assert second.duplicate is True
    assert fill_row_count(db_session) == 1
    order_count = db_session.execute(
        select(func.count()).select_from(ExecutionOrder)
    ).scalar_one()
    assert order_count == 1


@pytest.mark.parametrize(
    "checkpoint,expected_status",
    [
        ("before_intent_commit", None),
        ("after_intent_commit", ExecutionOrderStatus.UNKNOWN),
        ("after_broker_response", ExecutionOrderStatus.UNKNOWN),
        ("before_fill_commit", ExecutionOrderStatus.UNKNOWN),
    ],
)
def test_crash_windows_leave_expected_recovery_state(
    manual_session,
    checkpoint,
    expected_status,
    paper_cost_config,
):
    account = create_paper_account(
        manual_session,
        name="crash-account",
        initial_balance=Decimal("100000"),
    )
    deployment = create_execution_deployment(
        manual_session,
        paper_account_id=account.id,
        name="crash-dep",
        identity=strategy_identity(),
        lifecycle=DeploymentLifecycle.RUNNING,
    )
    token = "crash-lease"
    acquire_worker_lease(
        manual_session,
        deployment_id=deployment.id,
        worker_id="worker-a",
        lease_token=token,
        ttl_seconds=60,
        now=datetime(2024, 6, 1, 15, 0, tzinfo=timezone.utc),
    )
    manual_session.commit()

    clock = FixedClock(datetime(2024, 6, 1, 15, 0, tzinfo=timezone.utc))
    quotes = FakeQuoteSource(
        quotes={"WIN$": (Decimal("130000"), Decimal("130010"))},
        timestamp=clock.now(),
    )
    commits: list[str] = []

    def track_commit(session):
        session.commit()
        commits.append("committed")

    service = _service(
        clock,
        quotes,
        crash=CrashInjector({checkpoint}),
        commit=track_commit,
    )

    with pytest.raises(RuntimeError, match="crash injected"):
        service.process_completed_bar(
            manual_session,
            deployment=deployment,
            eval_result=_buy_result(deployment.id),
            lease_token=token,
            worker_id="worker-a",
            cost_config=paper_cost_config,
            point_value=Decimal("0.2"),
        )

    manual_session.rollback()
    manual_session.expire_all()
    orders = list_orders_for_deployment(manual_session, deployment.id)
    if expected_status is None:
        assert orders == []
    elif expected_status == ExecutionOrderStatus.UNKNOWN:
        assert len(orders) == 1
        if orders[0].status == ExecutionOrderStatus.UNKNOWN.value:
            return
        assert orders[0].status == ExecutionOrderStatus.INTENT.value
        mark_incomplete_orders_unknown(manual_session, deployment.id)
        manual_session.commit()
        manual_session.expire_all()
        refreshed = list_orders_for_deployment(manual_session, deployment.id)
        assert refreshed[0].status == ExecutionOrderStatus.UNKNOWN.value
    else:
        assert len(orders) == 1
        assert orders[0].status == expected_status.value


def test_pause_blocks_new_orders_but_retains_position(
    db_session, seeded_deployment, paper_cost_config
):
    _, deployment, token = seeded_deployment
    clock = FixedClock(datetime(2024, 6, 1, 15, 0, tzinfo=timezone.utc))
    quotes = FakeQuoteSource(
        quotes={"WIN$": (Decimal("130000"), Decimal("130010"))},
        timestamp=clock.now(),
    )
    service = _service(clock, quotes)
    service.process_completed_bar(
        db_session,
        deployment=deployment,
        eval_result=_buy_result(deployment.id),
        lease_token=token,
        worker_id="worker-a",
        cost_config=paper_cost_config,
        point_value=Decimal("0.2"),
    )
    transition_deployment_lifecycle(
        db_session, deployment.id, DeploymentLifecycle.PAUSED
    )
    db_session.refresh(deployment)
    result = service.process_completed_bar(
        db_session,
        deployment=deployment,
        eval_result=_buy_result(
            deployment.id,
            bar_close_time=datetime(2024, 6, 1, 15, 0, tzinfo=timezone.utc),
        ),
        lease_token=token,
        worker_id="worker-a",
        cost_config=paper_cost_config,
        point_value=Decimal("0.2"),
    )
    assert result.outcome == DecisionOutcome.RISK_REJECTED
    position = get_open_net_position(db_session, deployment.id)
    assert position is not None and position.is_open


def test_kill_switch_blocks_entries(db_session, seeded_deployment, paper_cost_config):
    _, deployment, token = seeded_deployment
    set_kill_switch(db_session, enabled=True, reason="test")
    clock = FixedClock(datetime(2024, 6, 1, 15, 0, tzinfo=timezone.utc))
    quotes = FakeQuoteSource(
        quotes={"WIN$": (Decimal("130000"), Decimal("130010"))},
        timestamp=clock.now(),
    )
    service = _service(clock, quotes)
    result = service.process_completed_bar(
        db_session,
        deployment=deployment,
        eval_result=_buy_result(deployment.id),
        lease_token=token,
        worker_id="worker-a",
        cost_config=paper_cost_config,
        point_value=Decimal("0.2"),
    )
    assert result.outcome == DecisionOutcome.RISK_REJECTED
    assert fill_row_count(db_session) == 0


def test_flatten_closes_open_position(db_session, seeded_deployment, paper_cost_config):
    _, deployment, token = seeded_deployment
    clock = FixedClock(datetime(2024, 6, 1, 15, 0, tzinfo=timezone.utc))
    quotes = FakeQuoteSource(
        quotes={"WIN$": (Decimal("130000"), Decimal("130010"))},
        timestamp=clock.now(),
    )
    service = _service(clock, quotes)
    service.process_completed_bar(
        db_session,
        deployment=deployment,
        eval_result=_buy_result(deployment.id),
        lease_token=token,
        worker_id="worker-a",
        cost_config=paper_cost_config,
        point_value=Decimal("0.2"),
    )
    result = service.flatten_deployment(
        db_session,
        deployment=deployment,
        lease_token=token,
        worker_id="worker-a",
        cost_config=paper_cost_config,
        point_value=Decimal("0.2"),
    )
    assert result.outcome == DecisionOutcome.ORDER_FILLED
    position = get_open_net_position(db_session, deployment.id)
    assert position is None or not position.is_open
