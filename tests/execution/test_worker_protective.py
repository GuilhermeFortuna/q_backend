"""Worker steps for timed exits and for working orders of deployments that must not trade (Q-137)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from q_backend.execution.brokers.base import BrokerOrderState, BrokerOrderLookupStatus
from q_backend.execution.brokers.fakes import FakeQuoteSource, FixedClock
from q_backend.execution.brokers.paper import PaperBroker
from q_backend.execution.domain import (
    BrokerMode,
    DeploymentLifecycle,
    ExecutionOrderStatus,
    ExecutionSide,
    FillRecord,
)
from q_backend.execution.ledger import ExecutionLedger
from q_backend.execution.recovery import DeploymentRuntime
from q_backend.execution.research_evaluator import ResearchEvaluator
from q_backend.execution.service import ExecutionService
from q_backend.execution.worker import ExecutionWorker
from q_backend.storage.db.execution_models import ExecutionNetPosition, ExecutionOrder
from q_backend.storage.db.execution_repositories import (
    acquire_worker_lease,
    create_execution_deployment,
    create_execution_order_intent,
    create_paper_account,
    get_open_net_position,
    set_open_position_timed_exit,
    transition_deployment_lifecycle,
)
from q_backend.storage.settings import Settings
from tests.execution.conftest import strategy_identity
from tests.execution.test_protective_orders import ScriptedBroker, _spec
from tests.execution.test_research_evaluator import SimpleResearchStrategy, _make_identity

NOW = datetime(2024, 6, 1, 15, 0, tzinfo=timezone.utc)
TOKEN = "lease-worker-protective"


def _worker(db_engine, *, broker) -> ExecutionWorker:
    quotes = FakeQuoteSource(
        quotes={"WIN$": (Decimal("130000"), Decimal("130010"))},
        timestamp=NOW,
    )
    ledger = ExecutionLedger()
    service = ExecutionService(
        broker=broker,
        quote_source=quotes,
        ledger=ledger,
        max_bar_age_seconds=7200.0,
        clock=lambda: NOW,
    )
    settings = Settings(
        execution_worker_id="worker-a",
        execution_lease_ttl_seconds=60,
        execution_default_point_value=0.2,
    )
    return ExecutionWorker(
        session_factory=sessionmaker(bind=db_engine, expire_on_commit=False),
        coordinator=None,
        quote_source=quotes,
        broker=broker,
        ledger=ledger,
        service=service,
        recovery=None,
        settings=settings,
        clock=lambda: NOW,
    )


def _seed(db_engine, *, open_position: bool):
    factory = sessionmaker(bind=db_engine, expire_on_commit=False)
    with factory() as session:
        account = create_paper_account(session, name="paper-worker", initial_balance=Decimal("100000"))
        deployment = create_execution_deployment(
            session,
            paper_account_id=account.id,
            name="win-worker",
            identity=strategy_identity(),
            lifecycle=DeploymentLifecycle.RUNNING,
        )
        acquire_worker_lease(
            session,
            deployment_id=deployment.id,
            worker_id="worker-a",
            lease_token=TOKEN,
            ttl_seconds=60,
            now=NOW,
        )
        if open_position:
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
                paper_account_id=account.id,
                deployment_id=deployment.id,
                order_id=order.id,
                fill=FillRecord(
                    broker_mode=BrokerMode.PAPER,
                    external_fill_id=f"seed-{order.id}",
                    side=ExecutionSide.BUY,
                    quantity=Decimal("1"),
                    price=Decimal("130000"),
                    filled_at=NOW - timedelta(hours=1),
                    metadata={},
                ),
                point_value=Decimal("0.2"),
                symbol="WIN$",
                position_opened_at=NOW - timedelta(hours=1),
            )
        session.commit()
        return account.id, deployment.id


def _runtime(session, deployment_id) -> DeploymentRuntime:
    from q_backend.storage.db.execution_repositories import get_execution_deployment

    deployment = get_execution_deployment(session, deployment_id)
    evaluator = ResearchEvaluator(
        deployment_id=str(deployment.id),
        identity=_make_identity(),
        strategy_instance=SimpleResearchStrategy(),
        window_bound=100,
    )
    return DeploymentRuntime(
        deployment=deployment,
        identity=_make_identity(),
        evaluator=evaluator,
        lease_token=TOKEN,
    )


def test_a_due_timed_exit_closes_the_position_at_the_next_poll(db_engine):
    _seed_account, deployment_id = _seed(db_engine, open_position=True)
    factory = sessionmaker(bind=db_engine, expire_on_commit=False)
    quotes = FakeQuoteSource(quotes={"WIN$": (Decimal("130000"), Decimal("130010"))}, timestamp=NOW)
    worker = _worker(db_engine, broker=PaperBroker(quote_source=quotes, clock=FixedClock(NOW)))
    with factory() as session:
        set_open_position_timed_exit(session, deployment_id, exit_at=NOW - timedelta(minutes=1))
        session.commit()
    worker._lease_tokens[deployment_id] = TOKEN

    with factory() as session:
        worker._run_due_timed_exit(session, _runtime(session, deployment_id), now=NOW)
        session.commit()

    with factory() as session:
        assert get_open_net_position(session, deployment_id) is None


def test_a_timed_exit_not_yet_due_leaves_the_position_open(db_engine):
    _seed_account, deployment_id = _seed(db_engine, open_position=True)
    factory = sessionmaker(bind=db_engine, expire_on_commit=False)
    quotes = FakeQuoteSource(quotes={"WIN$": (Decimal("130000"), Decimal("130010"))}, timestamp=NOW)
    worker = _worker(db_engine, broker=PaperBroker(quote_source=quotes, clock=FixedClock(NOW)))
    with factory() as session:
        set_open_position_timed_exit(session, deployment_id, exit_at=NOW + timedelta(hours=2))
        session.commit()
    worker._lease_tokens[deployment_id] = TOKEN

    with factory() as session:
        worker._run_due_timed_exit(session, _runtime(session, deployment_id), now=NOW)
        session.commit()

    with factory() as session:
        position = get_open_net_position(session, deployment_id)
        assert position is not None and position.quantity == Decimal("1")


def test_a_paused_deployment_gives_up_its_working_orders(db_engine):
    _seed_account, deployment_id = _seed(db_engine, open_position=False)
    factory = sessionmaker(bind=db_engine, expire_on_commit=False)
    broker = ScriptedBroker()
    worker = _worker(db_engine, broker=broker)
    from q_backend.execution.protective import ProtectiveOrders

    with factory() as session:
        deployment = _runtime(session, deployment_id).deployment
        ProtectiveOrders(
            broker=broker,
            ledger=ExecutionLedger(),
            clock=lambda: NOW,
            commit=lambda s: s.flush(),
            risk_check=lambda *a, **k: None,
        ).place_pending(
            session,
            deployment=deployment,
            spec=_spec(),
            quantity=Decimal("1"),
            bar_close_time=NOW,
            decision_id=None,
            lease_token=TOKEN,
            worker_id="worker-a",
            cost_config=None,
            point_value=Decimal("0.2"),
        )
        session.commit()

    with factory() as session:
        transition_deployment_lifecycle(session, deployment_id, DeploymentLifecycle.PAUSED)
        session.commit()

    with factory() as session:
        worker._runtimes[deployment_id] = _runtime(session, deployment_id)
        worker._reconcile_working_orders(session, now=NOW)
        session.commit()

    assert broker.named("cancel"), "the paused deployment's working entry must be removed at the broker"
    with factory() as session:
        remaining = (
            session.execute(
                select(ExecutionOrder).where(
                    ExecutionOrder.deployment_id == deployment_id,
                    ExecutionOrder.status == ExecutionOrderStatus.WORKING.value,
                )
            )
            .scalars()
            .all()
        )
        assert remaining == []


def test_recovery_hands_the_stored_levels_to_the_evaluator_trade(db_engine):
    from q_backend.execution.recovery import open_trade_for_deployment
    from q_backend.storage.db.execution_repositories import get_execution_deployment, set_open_position_protection

    _seed_account, deployment_id = _seed(db_engine, open_position=True)
    factory = sessionmaker(bind=db_engine, expire_on_commit=False)
    with factory() as session:
        set_open_position_protection(
            session,
            deployment_id,
            stop_loss=Decimal("129900"),
            take_profit=Decimal("130400"),
            external_position_id="42",
        )
        session.commit()

    with factory() as session:
        deployment = get_execution_deployment(session, deployment_id)
        trade = open_trade_for_deployment(session, deployment, point_value=0.2)

    assert trade is not None
    assert trade.stop_loss == 129900.0
    assert trade.take_profit == 130400.0


class _FlatteningRecovery:
    """Rebuilds the runtime as recovery does when a day-trade position outlived its session."""

    def __init__(self) -> None:
        self.now = None

    def build_runtime(self, session, deployment, *, worker_id, lease_token, point_value, initial_capital, now=None):
        self.now = now
        runtime = _runtime(session, deployment.id)
        runtime.needs_session_flatten = True
        runtime.config_revision = deployment.config_revision
        runtime.activation_cutoff_at = deployment.activation_cutoff_at
        return runtime


def test_a_rebuilt_runtime_flattens_a_position_left_from_a_closed_session(db_engine):
    _seed_account, deployment_id = _seed(db_engine, open_position=True)
    factory = sessionmaker(bind=db_engine, expire_on_commit=False)
    quotes = FakeQuoteSource(quotes={"WIN$": (Decimal("130000"), Decimal("130010"))}, timestamp=NOW)
    worker = _worker(db_engine, broker=PaperBroker(quote_source=quotes, clock=FixedClock(NOW)))
    recovery = _FlatteningRecovery()
    worker.recovery = recovery

    with factory() as session:
        stale = _runtime(session, deployment_id)
        stale.config_revision = stale.deployment.config_revision - 1
        worker._runtimes[deployment_id] = stale
        worker._lease_tokens[deployment_id] = TOKEN
        worker._refresh_deployments(session, now=NOW)
        session.commit()

    assert recovery.now == NOW
    with factory() as session:
        assert get_open_net_position(session, deployment_id) is None


def test_a_paused_deployment_still_records_a_broker_side_exit(db_engine):
    _seed_account, deployment_id = _seed(db_engine, open_position=True)
    factory = sessionmaker(bind=db_engine, expire_on_commit=False)
    broker = ScriptedBroker()
    broker.exit_state = BrokerOrderState(
        status=BrokerOrderLookupStatus.FILLED,
        fill=FillRecord(
            broker_mode=BrokerMode.PAPER,
            external_fill_id="exit-1",
            side=ExecutionSide.SELL,
            quantity=Decimal("1"),
            price=Decimal("129900"),
            filled_at=NOW,
            metadata={"exit_reason": "stop_loss"},
        ),
        external_position_id="42",
    )
    worker = _worker(db_engine, broker=broker)
    from q_backend.storage.db.execution_repositories import set_open_position_protection

    with factory() as session:
        set_open_position_protection(
            session, deployment_id, stop_loss=Decimal("129900"), take_profit=None, external_position_id="42"
        )
        transition_deployment_lifecycle(session, deployment_id, DeploymentLifecycle.PAUSED)
        session.commit()

    with factory() as session:
        worker._runtimes[deployment_id] = _runtime(session, deployment_id)
        worker._reconcile_working_orders(session, now=NOW)
        session.commit()

    with factory() as session:
        assert get_open_net_position(session, deployment_id) is None
