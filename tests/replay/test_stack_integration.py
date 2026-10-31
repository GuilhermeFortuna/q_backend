"""Opt-in integration: the normal execution path against the replay edge with PostgreSQL (Q-145 step 8).

Requires an isolated PostgreSQL (never the ./dev default). Run with:
    Q_CI_ISOLATED=1 DATABASE_URL=postgresql+psycopg://q:q@localhost:5436/q \
        uv run pytest tests/replay/test_stack_integration.py -m integration
The schema is created and dropped in that database.
"""

from __future__ import annotations

import os
import uuid
from datetime import timezone
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from q_backend.execution.brokers.base import MarketOrderRequest, PaperCostConfig
from q_backend.execution.brokers.edge import EdgeBroker
from q_backend.execution.brokers.fakes import FixedClock
from q_backend.execution.brokers.live_gates import LiveExecutionGates
from q_backend.execution.domain import (
    BrokerMode,
    DeploymentLifecycle,
    ExecutionOrderStatus,
    ExecutionSide,
)
from q_backend.execution.edge_client import EdgeClient, EdgeTimeouts
from q_backend.replay.edge import create_edge_app
from q_backend.replay.gateway import ServerThread
from q_backend.replay.journal import ReplayRunStore
from q_backend.replay.service import Driver, ReplaySimulator
from q_backend.storage.db.base import Base
from q_backend.storage.db import execution_models, models  # noqa: F401 - register tables
from q_backend.storage.db.execution_models import ExecutionOrder
from q_backend.storage.db.execution_repositories import (
    create_execution_deployment,
    create_execution_order_intent,
    create_paper_account,
    transition_execution_order,
)
from tests.execution.conftest import strategy_identity
from tests.replay.support import BASE, QUOTE, config_for, ms, spec, write_dataset

pytestmark = pytest.mark.integration

SYMBOL = "WIN$N"
LOGIN = 900001


@pytest.fixture
def pg_session():
    url = os.environ.get("DATABASE_URL", "")
    if not url.startswith("postgresql"):
        pytest.skip("DATABASE_URL must point at an isolated PostgreSQL")
    engine = create_engine(url)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    yield session
    session.close()
    Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture
def replay_edge(tmp_path):
    book = [(ms(-1) + step * 50, 99.0, 100.0, 0.0, 0.0, QUOTE) for step in range(0, 2600)]
    write_dataset(tmp_path, {SYMBOL: book}, {SYMBOL: spec()})
    store = ReplayRunStore(tmp_path / "pg-stack.sqlite3")
    simulator = ReplaySimulator(store, dataset_root=tmp_path)
    latency = {"order_transport_ms": 50, "broker_accept_ms": 50, "broker_execution_ms": 100, "broker_response_ms": 100}
    simulator.create(config_for(latency=latency, end_seconds=600), run_id="pg-1", generation="gen-pg")
    simulator.command(command_id=str(uuid.uuid4()), expected_revision=0, action="resume")
    driver = Driver(simulator, interval_s=0.002)
    driver.start()
    server = ServerThread(create_edge_app(simulator))
    url = server.start()
    yield simulator, url
    driver.stop()
    server.stop()
    store.close()


def test_order_intent_fills_through_the_replay_edge_and_persists(pg_session, replay_edge) -> None:
    simulator, url = replay_edge
    account = create_paper_account(pg_session, name="pg-replay", initial_balance=Decimal("100000"))
    deployment = create_execution_deployment(
        pg_session,
        paper_account_id=account.id,
        name="pg-replay-win",
        identity=strategy_identity(),
        broker_mode=BrokerMode.MT5_LIVE,
        lifecycle=DeploymentLifecycle.RUNNING,
        live_activation_enabled=True,
    )
    order = create_execution_order_intent(
        pg_session,
        deployment_id=deployment.id,
        decision_id=None,
        broker_mode=BrokerMode.MT5_LIVE,
        side=ExecutionSide.BUY,
        quantity=Decimal("1"),
        intent_committed_at=BASE.replace(tzinfo=timezone.utc),
    )
    pg_session.commit()

    client = EdgeClient(url, timeouts=EdgeTimeouts(connect_s=2.0, read_s=10.0, submit_read_s=10.0))
    broker = EdgeBroker(
        client=client,
        clock=FixedClock(BASE.replace(tzinfo=timezone.utc)),
        gates=LiveExecutionGates(
            enabled=True,
            account_allowlist=frozenset({LOGIN}),
            deployment_live_activation_enabled=True,
            controlled_account_validated=False,
        ),
    )
    request = MarketOrderRequest(
        deployment_id=deployment.id,
        order_id=order.id,
        symbol=SYMBOL,
        side=ExecutionSide.BUY,
        quantity=Decimal("1"),
        external_fill_id=str(order.id),
        broker_mode=BrokerMode.MT5_LIVE,
        intent_created_at=BASE.replace(tzinfo=timezone.utc),
        live_activation_enabled=True,
    )
    cost = PaperCostConfig(point_value=Decimal("1"), slippage_points=Decimal("0"), max_quote_age_seconds=60.0)
    result = broker.submit_market_order(request, cost_config=cost)
    assert result.outcome.value == "filled", result.rejection
    transition_execution_order(pg_session, order.id, ExecutionOrderStatus.SUBMITTED)
    transition_execution_order(
        pg_session, order.id, ExecutionOrderStatus.FILLED, external_order_id=result.external_order_id
    )
    pg_session.commit()

    persisted = pg_session.execute(select(ExecutionOrder).where(ExecutionOrder.id == order.id)).scalar_one()
    assert persisted.status == ExecutionOrderStatus.FILLED.value

    # A repeated submission of the same intent is refused by the edge and never reaches the broker.
    again = client._submit_body(
        order.id,
        {"intent_id": str(order.id), "order": {"symbol": SYMBOL, "side": "buy", "volume": 1.0}},  # noqa: SLF001
    )
    assert again["outcome"] == "indeterminate" and "already received" in again["reason"]
    deals = simulator.broker().deals(simulator.status().canonical_cursor_us, 0, 2**62)
    assert len(deals) == 1
