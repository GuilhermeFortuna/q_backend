"""Queued flatten actions are processed independently of deployment lifecycle."""

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from q_backend.execution.brokers.fakes import FakeQuoteSource, FixedClock
from q_backend.execution.brokers.paper import PaperBroker
from q_backend.execution.domain import (
    BrokerMode,
    DeploymentLifecycle,
    ExecutionSide,
    FillRecord,
)
from q_backend.execution.ledger import ExecutionLedger
from q_backend.execution.service import ExecutionService
from q_backend.execution.worker import ExecutionWorker
from q_backend.api.schemas.execution import DeploymentActionRequest
from q_backend.api.services.execution import apply_deployment_action
from q_backend.storage.db.execution_models import ExecutionRiskEvent
from q_backend.storage.db.execution_repositories import (
    acquire_worker_lease,
    create_execution_deployment,
    create_execution_order_intent,
    create_paper_account,
    get_open_net_position,
    release_worker_lease,
    set_pending_deployment_action,
)
from q_backend.storage.settings import Settings
from tests.execution.conftest import strategy_identity

NOW = datetime(2024, 6, 1, 15, 0, tzinfo=timezone.utc)


def _seed(db_engine, *, lifecycle=DeploymentLifecycle.STOPPED, position=True):
    factory = sessionmaker(bind=db_engine, expire_on_commit=False)
    with factory() as session:
        account = create_paper_account(session, name="flatten-paper", initial_balance=Decimal("100000"))
        deployment = create_execution_deployment(
            session,
            paper_account_id=account.id,
            name="flatten-deployment",
            identity=strategy_identity(),
            lifecycle=lifecycle,
        )
        if position:
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
                    filled_at=NOW,
                    metadata={},
                ),
                point_value=Decimal("0.2"),
                symbol="WIN$",
                position_opened_at=NOW,
            )
        set_pending_deployment_action(session, deployment.id, action="flatten", at=NOW)
        session.commit()
    return factory, deployment


def _worker(db_engine, *, quote=True):
    clock = FixedClock(NOW)
    quotes = FakeQuoteSource(quotes={"WIN$": (Decimal("130000"), Decimal("130010"))} if quote else {}, timestamp=NOW)
    ledger = ExecutionLedger()
    broker = PaperBroker(quote_source=quotes, clock=clock)
    service = ExecutionService(broker=broker, quote_source=quotes, ledger=ledger, clock=clock.now)
    settings = Settings(execution_worker_id="flatten-worker", execution_lease_ttl_seconds=60)
    return ExecutionWorker(
        session_factory=sessionmaker(bind=db_engine, expire_on_commit=False),
        coordinator=None,
        quote_source=quotes,
        broker=broker,
        ledger=ledger,
        service=service,
        recovery=None,
        settings=settings,
        clock=clock.now,
    )


def test_stopped_deployment_flattens_and_can_be_archived(db_engine):
    factory, deployment = _seed(db_engine)
    worker = _worker(db_engine)
    worker.poll_once()
    with factory() as session:
        current = session.get(type(deployment), deployment.id)
        position = get_open_net_position(session, deployment.id)
        assert current.lifecycle == DeploymentLifecycle.STOPPED.value
        assert current.pending_action is None
        assert position is None or not position.is_open
        archived = apply_deployment_action(
            session,
            deployment.id,
            DeploymentActionRequest(action="archive", confirm=True),
        )
        assert archived.message == "deployment archived"


def test_paused_deployment_flattens_without_running_deployment(db_engine):
    factory, deployment = _seed(db_engine, lifecycle=DeploymentLifecycle.PAUSED)
    _worker(db_engine).poll_once()
    with factory() as session:
        current = session.get(type(deployment), deployment.id)
        assert current.lifecycle == DeploymentLifecycle.PAUSED.value
        assert current.pending_action is None


def test_stopped_flatten_without_position_clears_without_quote(db_engine):
    factory, deployment = _seed(db_engine, position=False)
    _worker(db_engine, quote=False).poll_once()
    with factory() as session:
        current = session.get(type(deployment), deployment.id)
        assert current.pending_action is None
        assert session.execute(select(ExecutionRiskEvent)).scalars().all() == []


def test_missing_quote_keeps_flatten_and_records_one_risk_event(db_engine):
    factory, deployment = _seed(db_engine)
    worker = _worker(db_engine, quote=False)
    worker.poll_once()
    worker.poll_once()
    with factory() as session:
        current = session.get(type(deployment), deployment.id)
        risks = session.execute(select(ExecutionRiskEvent)).scalars().all()
        assert current.pending_action == "flatten"
        assert len(risks) == 1
        assert "quote" in risks[0].message


def test_deployment_with_other_workers_lease_is_skipped_until_released(db_engine):
    factory, deployment = _seed(db_engine)
    with factory() as session:
        acquire_worker_lease(
            session,
            deployment_id=deployment.id,
            worker_id="other-worker",
            lease_token="other-token",
            ttl_seconds=60,
            now=NOW,
        )
        session.commit()
    worker = _worker(db_engine)
    worker.poll_once()
    with factory() as session:
        assert session.get(type(deployment), deployment.id).pending_action == "flatten"
        release_worker_lease(
            session,
            deployment_id=deployment.id,
            worker_id="other-worker",
            lease_token="other-token",
        )
        session.commit()
    worker.poll_once()
    with factory() as session:
        assert session.get(type(deployment), deployment.id).pending_action is None
