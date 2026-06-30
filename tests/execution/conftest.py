from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

import pytest

from q_backend.execution.brokers.base import PaperCostConfig
from q_backend.execution.brokers.fakes import FakeQuoteSource, FixedClock
from q_backend.execution.brokers.paper import PaperBroker
from q_backend.execution.domain import DeploymentLifecycle, StrategyIdentity
from q_backend.execution.ledger import ExecutionLedger
from q_backend.execution.service import ExecutionService
from q_backend.storage.db.base import Base
from q_backend.storage.db import models  # noqa: F401
from q_backend.storage.db import execution_models  # noqa: F401
from q_backend.storage.db.execution_repositories import (
    acquire_worker_lease,
    create_execution_deployment,
    create_paper_account,
)


def strategy_identity(
    *,
    symbol: str = "WIN$",
    timeframe: str = "H1",
) -> StrategyIdentity:
    return StrategyIdentity(
        strategy_name="MACrossover",
        strategy_version=1,
        compiled_config={
            "strategy": "MACrossover",
            "strategy_params": {"fast_period": 5, "slow_period": 20},
            "symbol": symbol,
            "timeframe": timeframe,
        },
        config_hash="test-hash",
        symbol=symbol,
        timeframe=timeframe,
        sizing_config={"type": "fixed_quantity", "quantity": 1.0},
        risk_config={"max_daily_loss": "5000", "max_notional": "1000000"},
    )


@pytest.fixture
def db_engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    yield engine
    Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture
def db_session(db_engine) -> Session:
    session_factory = sessionmaker(
        bind=db_engine,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
    )
    session = session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@pytest.fixture
def execution_clock() -> FixedClock:
    return FixedClock(datetime(2024, 6, 1, 15, 0, tzinfo=timezone.utc))


@pytest.fixture
def execution_quote_source(execution_clock: FixedClock) -> FakeQuoteSource:
    return FakeQuoteSource(
        quotes={"WIN$": (Decimal("130000"), Decimal("130010"))},
        timestamp=execution_clock.now(),
    )


@pytest.fixture
def paper_cost_config() -> PaperCostConfig:
    return PaperCostConfig(
        point_value=Decimal("0.2"),
        slippage_points=Decimal("0"),
        max_quote_age_seconds=60.0,
    )


@pytest.fixture
def execution_service(
    execution_quote_source: FakeQuoteSource,
    execution_clock: FixedClock,
) -> ExecutionService:
    broker = PaperBroker(quote_source=execution_quote_source, clock=execution_clock)
    return ExecutionService(
        broker=broker,
        quote_source=execution_quote_source,
        ledger=ExecutionLedger(),
        max_bar_age_seconds=7200.0,
        clock=execution_clock.now,
    )


@pytest.fixture
def seeded_deployment(db_session: Session):
    account = create_paper_account(
        db_session,
        name="paper-test",
        initial_balance=Decimal("100000"),
    )
    deployment = create_execution_deployment(
        db_session,
        paper_account_id=account.id,
        name="win-h1-test",
        identity=strategy_identity(),
        lifecycle=DeploymentLifecycle.RUNNING,
    )
    token = "lease-token-1"
    acquire_worker_lease(
        db_session,
        deployment_id=deployment.id,
        worker_id="worker-a",
        lease_token=token,
        ttl_seconds=60,
        now=datetime(2024, 6, 1, 15, 0, tzinfo=timezone.utc),
    )
    db_session.flush()
    return account, deployment, token


@pytest.fixture
def manual_session(db_engine) -> Session:
    factory = sessionmaker(
        bind=db_engine,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
    )
    session = factory()
    try:
        yield session
    finally:
        session.close()
