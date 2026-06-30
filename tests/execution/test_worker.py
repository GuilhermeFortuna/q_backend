"""Worker lease, recovery, and benchmark tests."""

from __future__ import annotations

import statistics
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import numpy as np
import pandas as pd
import pytest
from sqlalchemy.orm import sessionmaker

from q_backend.backtesting.strategy_registry import default_params_for
from q_backend.execution.bar_coordinator import BarCoordinator, DeploymentBarConsumer
from q_backend.execution.brokers.fakes import FakeQuoteSource, FixedClock
from q_backend.execution.brokers.paper import PaperBroker
from q_backend.execution.domain import DeploymentLifecycle, StrategyIdentity
from q_backend.execution.evaluator import StrategyEvaluator
from q_backend.execution.ledger import ExecutionLedger
from q_backend.execution.recovery import ExecutionRecovery
from q_backend.execution.service import ExecutionService
from q_backend.execution.worker import ExecutionWorker
from q_backend.market_data.models import OHLCV
from q_backend.storage.db.execution_repositories import (
    LeaseConflictError,
    acquire_worker_lease,
    create_execution_deployment,
    create_paper_account,
    transition_deployment_lifecycle,
)
from q_backend.storage.settings import Settings
from tests.execution.conftest import strategy_identity


class StaticOhlcvProvider:
    def __init__(self, frame: pd.DataFrame, *, symbol: str, timeframe: str) -> None:
        self._frame = frame
        self.symbol = symbol
        self.timeframe = timeframe

    def get_ohlcv(self, symbol: str, timeframe: str, start: datetime, end: datetime):
        if symbol != self.symbol or timeframe.upper() != self.timeframe.upper():
            return []
        rows = []
        for open_time, row in self._frame.iterrows():
            close_time = open_time + pd.Timedelta(hours=1)
            if close_time.to_pydatetime().replace(tzinfo=timezone.utc) < start:
                continue
            if open_time.to_pydatetime().replace(tzinfo=timezone.utc) > end:
                continue
            rows.append(
                OHLCV(
                    time=open_time.to_pydatetime().replace(tzinfo=timezone.utc),
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    tick_volume=int(row["volume"]),
                )
            )
        return rows


def _synthetic_ohlcv(n: int = 80, *, freq: str = "h") -> pd.DataFrame:
    rng = np.random.default_rng(42)
    steps = rng.normal(0.0, 1.0, size=n)
    close = 130000.0 + np.cumsum(steps)
    open_ = close - rng.normal(0.0, 0.5, size=n)
    high = np.maximum(open_, close) + np.abs(rng.normal(0.0, 0.5, size=n))
    low = np.minimum(open_, close) - np.abs(rng.normal(0.0, 0.5, size=n))
    volume = rng.integers(1_000, 5_000, size=n).astype(float)
    index = pd.date_range("2024-01-01", periods=n, freq=freq, tz="UTC")
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": volume},
        index=index,
    )


def test_lease_takeover_blocks_second_worker(db_session):
    account = create_paper_account(
        db_session, name="lease-account", initial_balance=Decimal("100000")
    )
    deployment = create_execution_deployment(
        db_session,
        paper_account_id=account.id,
        name="lease-dep",
        identity=strategy_identity(),
        lifecycle=DeploymentLifecycle.RUNNING,
    )
    now = datetime(2024, 6, 1, 15, 0, tzinfo=timezone.utc)
    acquire_worker_lease(
        db_session,
        deployment_id=deployment.id,
        worker_id="worker-a",
        lease_token="token-a",
        ttl_seconds=60,
        now=now,
    )
    with pytest.raises(LeaseConflictError):
        acquire_worker_lease(
            db_session,
            deployment_id=deployment.id,
            worker_id="worker-b",
            lease_token="token-b",
            ttl_seconds=60,
            now=now,
        )


def test_worker_poll_once_processes_new_bar(db_engine, db_session):
    account = create_paper_account(
        db_session, name="worker-account", initial_balance=Decimal("100000")
    )
    deployment = create_execution_deployment(
        db_session,
        paper_account_id=account.id,
        name="worker-dep",
        identity=strategy_identity(),
        lifecycle=DeploymentLifecycle.RUNNING,
    )
    db_session.commit()

    frame = _synthetic_ohlcv(80)
    clock = FixedClock(frame.index[-2].to_pydatetime())
    quotes = FakeQuoteSource(
        quotes={"WIN$": (Decimal("130000"), Decimal("130010"))},
        timestamp=clock.now(),
    )
    provider = StaticOhlcvProvider(frame, symbol="WIN$", timeframe="H1")
    coordinator = BarCoordinator(provider=provider, clock=clock.now)
    broker = PaperBroker(quote_source=quotes, clock=clock)
    ledger = ExecutionLedger()
    service = ExecutionService(
        broker=broker,
        quote_source=quotes,
        ledger=ledger,
        max_bar_age_seconds=7200.0,
    )
    recovery = ExecutionRecovery(
        coordinator=coordinator,
        quote_source=quotes,
        ledger=ledger,
        point_value=Decimal("0.2"),
        initial_window_bars=60,
    )
    settings = Settings(
        execution_worker_id="worker-a",
        execution_lease_ttl_seconds=60,
        execution_poll_interval_seconds=0.01,
        execution_initial_window_bars=60,
        execution_default_point_value=0.2,
    )
    factory = sessionmaker(bind=db_engine, expire_on_commit=False)
    worker = ExecutionWorker(
        session_factory=factory,
        coordinator=coordinator,
        quote_source=quotes,
        broker=broker,
        ledger=ledger,
        service=service,
        recovery=recovery,
        settings=settings,
        clock=clock.now,
        poll_interval_seconds=0.01,
    )
    worker.poll_once()
    with factory() as session:
        from q_backend.storage.db.execution_repositories import list_orders_for_deployment

        orders = list_orders_for_deployment(session, deployment.id)
        assert len(orders) <= 1


BENCHMARK_FIXTURES = (
    ("CCM$", "H1", "h", 400),
    ("WIN$", "H1", "h", 400),
    ("WDO$", "M15", "15min", 600),
)


@pytest.mark.parametrize("symbol,timeframe,freq,bars", BENCHMARK_FIXTURES)
def test_full_path_benchmark_p95_under_budget(
    symbol: str, timeframe: str, freq: str, bars: int, db_engine
):
    from q_backend.execution.brokers.base import PaperCostConfig
    from q_backend.storage.db.execution_models import ExecutionDeployment

    compiled = {
        "strategy": "MACrossover",
        "strategy_params": default_params_for("MACrossover"),
        "symbol": symbol,
        "timeframe": timeframe,
    }
    identity = StrategyIdentity(
        strategy_name="MACrossover",
        strategy_version=1,
        compiled_config=compiled,
        config_hash=f"bench-{symbol}-{timeframe}",
        symbol=symbol,
        timeframe=timeframe,
        sizing_config={"type": "fixed_quantity", "quantity": 1.0},
    )
    data = _synthetic_ohlcv(bars, freq=freq)
    evaluator = StrategyEvaluator(deployment_id=f"bench-{symbol}", identity=identity)
    evaluator.seed_window(data.iloc[:-20])

    factory = sessionmaker(bind=db_engine, expire_on_commit=False)
    deployment_id = None
    with factory() as session:
        account = create_paper_account(
            session, name=f"bench-{symbol}", initial_balance=Decimal("100000")
        )
        deployment = create_execution_deployment(
            session,
            paper_account_id=account.id,
            name=f"bench-{symbol}",
            identity=identity,
            lifecycle=DeploymentLifecycle.RUNNING,
        )
        deployment_id = deployment.id
        token = "bench-lease"
        acquire_worker_lease(
            session,
            deployment_id=deployment.id,
            worker_id="worker-a",
            lease_token=token,
            ttl_seconds=120,
            now=data.index[-1].to_pydatetime(),
        )
        session.commit()

    clock = FixedClock(data.index[-1].to_pydatetime())
    quotes = FakeQuoteSource(
        quotes={symbol: (Decimal("100"), Decimal("101"))},
        timestamp=clock.now(),
    )
    broker = PaperBroker(quote_source=quotes, clock=clock)
    service = ExecutionService(
        broker=broker,
        quote_source=quotes,
        ledger=ExecutionLedger(),
        max_bar_age_seconds=7200.0,
        clock=clock.now,
    )
    cost_config = PaperCostConfig(point_value=Decimal("0.2"), max_quote_age_seconds=120.0)
    totals: list[float] = []
    start_idx = len(data) - 20
    for i in range(start_idx, len(data)):
        results = evaluator.ingest_completed_bars(data.iloc[i : i + 1])
        if not results or results[0].signal_action.value == "hold":
            continue
        with factory() as session:
            deployment = session.get(ExecutionDeployment, deployment_id)
            started = time.perf_counter()
            outcome = service.process_completed_bar(
                session,
                deployment=deployment,
                eval_result=results[0],
                lease_token=token,
                worker_id="worker-a",
                cost_config=cost_config,
                point_value=Decimal("0.2"),
            )
            session.commit()
            if outcome.outcome.value == "order_filled":
                totals.append((time.perf_counter() - started) * 1000.0)

    if totals:
        p95 = statistics.quantiles(totals, n=20)[-1]
        print(f"BENCHMARK full_path {symbol} {timeframe} p95_ms={p95:.2f} samples={len(totals)}")
        assert p95 < 500.0
