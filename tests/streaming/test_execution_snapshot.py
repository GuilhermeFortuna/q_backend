from datetime import datetime, timezone
from decimal import Decimal
import uuid
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from fastapi.testclient import TestClient

from q_backend.api.main import app
from q_backend.execution.domain import (
    BrokerMode,
    DecisionOutcome,
    ExecutionSide,
    RiskRejectionCode,
    SignalAction,
    StrategyIdentity,
)
from q_backend.storage.db.base import Base
from q_backend.storage.db.outbox_models import OutboxTopicState
from q_backend.storage.db.execution_repositories import (
    create_execution_decision,
    create_execution_deployment,
    create_execution_fill,
    create_execution_order_intent,
    create_paper_account,
    record_risk_event,
    set_kill_switch,
)
from q_backend.streaming import execution_events
from q_backend.streaming.snapshot import read_execution_snapshot
from tests.streaming.replay_schema import assert_valid_replay


def _identity() -> StrategyIdentity:
    return StrategyIdentity(
        strategy_name="snap_strategy",
        strategy_version=1,
        compiled_config={"threshold": 0.5},
        config_hash="snap_hash",
        symbol="WIN$",
        timeframe="5m",
        sizing_config={"fixed_qty": 1.0},
        risk_config={"max_loss": 500.0},
    )


@pytest.fixture
def snapshot_session_factory():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(
        bind=engine,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
    )
    yield factory
    Base.metadata.drop_all(engine)
    engine.dispose()


def test_execution_snapshot_contract_and_watermark(snapshot_session_factory):
    with snapshot_session_factory() as session:
        account = create_paper_account(session, name="acc-snap-1", initial_balance=Decimal("100000.00"))
        deployment = create_execution_deployment(
            session,
            paper_account_id=account.id,
            name="dep-snap-1",
            identity=_identity(),
        )
        decision = create_execution_decision(
            session,
            deployment_id=deployment.id,
            bar_close_time=datetime(2026, 9, 18, 14, 0, tzinfo=timezone.utc),
            identity=_identity(),
            signal_action=SignalAction.BUY,
            outcome=DecisionOutcome.SIGNAL,
            requested_quantity=Decimal("1.0"),
        )
        order = create_execution_order_intent(
            session,
            deployment_id=deployment.id,
            decision_id=decision.id,
            broker_mode=BrokerMode.PAPER,
            side=ExecutionSide.BUY,
            quantity=Decimal("1.0"),
        )
        create_execution_fill(
            session,
            deployment_id=deployment.id,
            order_id=order.id,
            broker_mode=BrokerMode.PAPER,
            external_fill_id="ext-snap-fill-1",
            side=ExecutionSide.BUY,
            quantity=Decimal("1.0"),
            price=Decimal("125000.00"),
            filled_at=datetime(2026, 9, 18, 14, 5, tzinfo=timezone.utc),
        )
        record_risk_event(
            session,
            deployment_id=deployment.id,
            rejection_code=RiskRejectionCode.DAILY_LOSS_LIMIT,
            message="Drawdown limit hit",
        )
        set_kill_switch(session, enabled=True, reason="Halt test", updated_by="admin")
        session.commit()

    snapshot = read_execution_snapshot(snapshot_session_factory)

    # Contract schema validation
    assert_valid_replay("execution-snapshot", snapshot)

    # Watermark equals per-topic maximum
    expected_topics = {"decisions", "orders", "fills", "risk", "ledger", "deployments"}
    assert set(snapshot["watermark"].keys()) == expected_topics
    for topic in expected_topics:
        cursor = snapshot["watermark"][topic]
        assert "epoch" in cursor
        assert "seq" in cursor
        assert cursor["seq"] >= 0

    assert snapshot["watermark"]["decisions"]["seq"] == 1
    assert snapshot["watermark"]["orders"]["seq"] == 1
    assert snapshot["watermark"]["fills"]["seq"] == 1
    assert snapshot["watermark"]["risk"]["seq"] == 2  # risk rejection + kill switch
    assert snapshot["watermark"]["deployments"]["seq"] == 1

    # Control state
    assert snapshot["control"]["kill_switch_enabled"] is True
    assert snapshot["control"]["kill_switch_reason"] == "Halt test"
    assert snapshot["control"]["updated_by"] == "admin"

    # Entities present
    assert len(snapshot["deployments"]) == 1
    assert len(snapshot["accounts"]) == 1
    assert len(snapshot["orders"]) == 1
    assert len(snapshot["recent"]["decisions"]) == 1
    assert len(snapshot["recent"]["fills"]) == 1
    assert len(snapshot["recent"]["risk"]) == 1  # only risk rejections in table


def test_execution_snapshot_isolation_concurrent_commit(tmp_path):
    from sqlalchemy.pool import NullPool

    db_path = tmp_path / "wal_iso.db"
    engine = create_engine(
        f"sqlite:///{db_path}",
        poolclass=NullPool,
        connect_args={"autocommit": True, "check_same_thread": False, "timeout": 30.0},
    )
    with engine.connect() as conn:
        conn.exec_driver_sql("PRAGMA journal_mode=WAL")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)

    with factory() as session:
        account = create_paper_account(session, name="acc-snap-iso", initial_balance=Decimal("100000.00"))
        deployment = create_execution_deployment(
            session,
            paper_account_id=account.id,
            name="dep-snap-iso",
            identity=_identity(),
        )
        order1 = create_execution_order_intent(
            session,
            deployment_id=deployment.id,
            decision_id=None,
            broker_mode=BrokerMode.PAPER,
            side=ExecutionSide.BUY,
            quantity=Decimal("1.0"),
        )
        session.commit()

    # Open connection and snapshot transaction A
    conn_a = engine.connect()
    conn_a.exec_driver_sql("BEGIN")
    conn_a.exec_driver_sql("SELECT 1 FROM execution_orders")  # establish WAL snapshot
    session_a = sessionmaker(bind=conn_a, autoflush=False, autocommit=False, expire_on_commit=False)()

    try:
        # Concurrent transaction B commits order2
        with factory() as session_b:
            order2 = create_execution_order_intent(
                session_b,
                deployment_id=deployment.id,
                decision_id=None,
                broker_mode=BrokerMode.PAPER,
                side=ExecutionSide.SELL,
                quantity=Decimal("2.0"),
            )
            session_b.commit()

        # In transaction A, read execution snapshot
        snapshot = read_execution_snapshot(session_factory=None, session=session_a)

        # Snapshot A must NOT see order2 or its watermark sequence
        order_ids = [o["id"] for o in snapshot["orders"]]
        assert str(order1.id) in order_ids
        assert str(order2.id) not in order_ids
        assert snapshot["watermark"]["orders"]["seq"] == 1
    finally:
        conn_a.exec_driver_sql("ROLLBACK")
        session_a.close()
        conn_a.close()


def test_execution_snapshot_route_200(snapshot_session_factory, monkeypatch):
    from q_backend.storage.db import engine as db_engine_module

    monkeypatch.setattr(db_engine_module, "create_session_factory", lambda: snapshot_session_factory)

    with snapshot_session_factory() as session:
        account = create_paper_account(session, name="acc-snap-route", initial_balance=Decimal("100000.00"))
        create_execution_deployment(
            session,
            paper_account_id=account.id,
            name="dep-snap-route",
            identity=_identity(),
        )
        session.commit()

    client = TestClient(app)
    response = client.get("/api/v1/stream/execution/snapshot")
    assert response.status_code == 200
    payload = response.json()
    assert_valid_replay("execution-snapshot", payload)
    assert len(payload["deployments"]) == 1


def test_execution_snapshot_route_503_on_db_down(monkeypatch):
    from q_backend.storage.db import engine as db_engine_module

    def _failing_factory():
        raise RuntimeError("Database connection refused")

    monkeypatch.setattr(db_engine_module, "create_session_factory", _failing_factory)

    client = TestClient(app)
    response = client.get("/api/v1/stream/execution/snapshot")
    assert response.status_code == 503
    payload = response.json()
    assert payload["code"] == "database_unavailable"
