"""Opt-in ordinary API → worker/evaluator → HTTP simulator → ledger → Redis integration.

Use disposable PostgreSQL/Redis endpoints with Q_CI_ISOLATED=1, DATABASE_URL and
REDIS_URL. Never use development databases: the fixture creates and drops schema,
and clears only Redis keys it creates in an initially empty database.
"""

from __future__ import annotations

import os
import time
import uuid
from datetime import timedelta
from decimal import Decimal

import httpx
import pytest
import redis
from fastapi import FastAPI
from sqlalchemy import create_engine, inspect, select
from sqlalchemy.orm import sessionmaker

from q_backend.api.deps import get_market_clock
from q_backend.api.dependencies import get_market_data_service
from q_backend.api.routers.execution import router
from q_backend.api.services import execution as execution_api
from q_backend.backtesting.strategy_registry import default_params_for
from q_backend.cli import q_execution
from q_backend.execution.validation import compute_config_hash
from q_backend.market_data.service import MarketDataService
from q_backend.market_data.timezone import to_brasilia_naive
from q_backend.replay.edge import create_edge_app
from q_backend.replay.gateway import ServerThread, create_gateway_app
from q_backend.replay.journal import ReplayRunStore
from q_backend.replay.service import Driver, ReplaySimulator
from q_backend.storage.db.base import Base
from q_backend.storage.db import execution_models, models  # noqa: F401 - register tables
from q_backend.storage.db.execution_models import ExecutionDecision, ExecutionFill, ExecutionLedgerEntry, ExecutionOrder
from q_backend.storage.db.outbox_models import OutboxEvent
from q_backend.storage.runtime_config import set_data_source
from q_backend.storage.settings import Settings
from q_backend.streaming.codec import decode_entry
from q_backend.streaming.keys import stream_key
from q_backend.streaming.market.publisher import MarketDataPublisher, MarketPublisherConfig
from q_backend.streaming.publisher import EphemeralPublisher
from q_backend.streaming.relay import OutboxRelay
from tests.replay.support import BASE, TRADE, config_for, ms, spec, write_dataset

pytestmark = pytest.mark.integration
SYMBOL = "WIN$N"


@pytest.fixture
def stack_storage():
    database_url = os.environ.get("DATABASE_URL", "")
    redis_url = os.environ.get("REDIS_URL", "")
    if not database_url.startswith("postgresql") or not redis_url:
        pytest.skip("DATABASE_URL and REDIS_URL must identify disposable PostgreSQL and Redis")
    if os.environ.get("Q_CI_ISOLATED") != "1":
        pytest.fail("Q_CI_ISOLATED=1 is required for destructive replay integration fixtures")
    if ":5434/" in database_url or ":6380/" in redis_url:
        pytest.fail("refusing development PostgreSQL/Redis endpoints")
    client = redis.Redis.from_url(redis_url)
    assert client.dbsize() == 0, "integration Redis database must be empty"
    engine = create_engine(database_url)
    assert not inspect(engine).get_table_names(), "integration PostgreSQL database must be empty"
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        yield factory, client
    finally:
        keys = list(client.scan_iter(match="q:*"))
        if keys:
            client.delete(*keys)
        client.close()
        Base.metadata.drop_all(engine)
        engine.dispose()


@pytest.fixture
def simulated_stack(tmp_path, monkeypatch, stack_storage):
    factory, redis_client = stack_storage
    # Public synthetic trade prices: declining warmup, then one decisive upward
    # crossover. Subsequent bars stay high, avoiding a second entry signal.
    book = [(ms(minute * 300), 99.0, 100.0, float(100 - minute), 1.0, TRADE | 2 | 4) for minute in range(-30, 0)]
    book += [(ms(second), 149.0, 150.0, 150.0, 1.0, TRADE | 2 | 4) for second in range(0, 1801)]
    write_dataset(
        tmp_path,
        {SYMBOL: book},
        {SYMBOL: spec(commission_per_contract="2.00")},
        coverage=(BASE - timedelta(minutes=150), BASE + timedelta(seconds=1801)),
    )
    store = ReplayRunStore(tmp_path / "stack.sqlite3")
    simulator = ReplaySimulator(store, dataset_root=tmp_path)
    config = config_for(
        end_seconds=1800,
        rate=10,
        latency={
            "order_transport_ms": 500,
            "broker_accept_ms": 500,
            "broker_execution_ms": 500,
            "broker_response_ms": 1000,
        },
    ).model_copy(update={"warmup_start_utc": BASE - timedelta(minutes=150)})
    simulator.create(config, run_id="stack", generation="stack-generation")
    gateway = ServerThread(create_gateway_app(market=simulator.market, ingress=simulator.gateway_ingress))
    edge = ServerThread(create_edge_app(simulator))
    gateway_url, edge_url = gateway.start(), edge.start()
    monkeypatch.setenv("Q_MT5_GATEWAY_URL", gateway_url)
    monkeypatch.setenv("Q_MARKET_CLOCK_URL", gateway_url)
    monkeypatch.setenv("Q_MARKET_CLOCK_GENERATION", "stack-generation")
    monkeypatch.setenv("Q_MARKET_DATA_CACHE_NAMESPACE", "stack-generation")
    monkeypatch.setenv("Q_TICK_CACHE_DIR", str(tmp_path / "tick-cache"))
    set_data_source("remote")
    settings = Settings(
        mt5_edge_url=edge_url,
        mt5_edge_read_timeout_s=2,
        mt5_edge_submit_timeout_s=0.02,
        live_execution_enabled=True,
        live_execution_account_allowlist="900001",
        execution_initial_window_bars=30,
        execution_default_point_value=1,
        execution_worker_id="stack-worker",
        execution_order_poll_interval_s=0,
    )
    monkeypatch.setattr(execution_api, "get_settings", lambda: settings)
    monkeypatch.setattr(q_execution, "create_session_factory", lambda: factory)
    market_data = MarketDataService()
    components = q_execution._build_components(market_data, settings=settings)
    worker = q_execution._build_worker(components, poll_interval_seconds=0)
    app = FastAPI()
    app.include_router(router)

    monkeypatch.setattr("q_backend.api.deps.create_session_factory", lambda: factory)
    monkeypatch.setattr("q_backend.api.idempotency.create_session_factory", lambda: factory)
    app.dependency_overrides[get_market_data_service] = lambda: market_data
    app.dependency_overrides[get_market_clock] = lambda: components.market
    api = ServerThread(app)
    driver = Driver(simulator, interval_s=0.002)
    driver.start()
    simulator.command(command_id=str(uuid.uuid4()), expected_revision=0, action="resume")
    try:
        with httpx.Client(base_url=api.start(), timeout=10) as http:
            yield simulator, components, worker, http
    finally:
        worker._shutdown()
        api.stop()
        driver.stop()
        gateway.stop()
        edge.stop()
        store.close()


def _command(http, path, body):
    response = http.post(path, json=body, headers={"Idempotency-Key": str(uuid.uuid4())})
    assert response.status_code == 200, response.text
    return response.json()


def _wait_until(predicate, timeout=10):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "timed out waiting for stack progress"
        time.sleep(0.01)


def test_normal_stack_reconciles_delayed_fill_and_publishes_it_once(stack_storage, simulated_stack):
    factory, redis_client = stack_storage
    simulator, components, worker, http = simulated_stack
    account = _command(http, "/api/v1/execution/accounts", {"name": "replay", "initial_balance": "100000"})
    params = {**default_params_for("MACrossover"), "short_period": 2, "long_period": 10}
    compiled = {"strategy": "MACrossover", "strategy_params": params, "symbol": SYMBOL, "timeframe": "M5"}
    deployment = _command(
        http,
        "/api/v1/execution/deployments",
        {
            "paper_account_id": account["id"],
            "name": "public-crossover",
            "broker_mode": "mt5_live",
            "live_activation_enabled": True,
            "identity": {
                "strategy_name": "MACrossover",
                "strategy_version": 1,
                "compiled_config": compiled,
                "config_hash": compute_config_hash(compiled),
                "symbol": SYMBOL,
                "timeframe": "M5",
                "sizing_config": {"type": "fixed_quantity", "quantity": 1},
                "risk_config": {"max_notional": "1000000", "max_daily_loss": "5000"},
            },
        },
    )
    _command(http, f"/api/v1/execution/deployments/{deployment['id']}/actions", {"action": "start"})
    worker.poll_once()  # real evaluator warms up after ordinary API activation
    publisher = MarketDataPublisher(
        components.market_data._remote_client,
        {
            topic: EphemeralPublisher(redis_client, topic, producer_id="market-publisher")
            for topic in ("quotes", "bars.forming", "bars.completed")
        },
        MarketPublisherConfig(symbols=(SYMBOL,), timeframes=("M5",)),
        clock=lambda: to_brasilia_naive(components.market.now()),
    )
    publisher.poll_bars_once()
    simulator.command(command_id=str(uuid.uuid4()), expected_revision=simulator.status().revision, action="pause")
    simulator.command(
        command_id=str(uuid.uuid4()), expected_revision=simulator.status().revision, action="step", market_ms=300000
    )
    simulator.command(command_id=str(uuid.uuid4()), expected_revision=simulator.status().revision, action="resume")
    # Wait for the configured remote clock to observe the external step.
    _wait_until(lambda: components.market.now() >= BASE + timedelta(seconds=300))
    publisher.poll_ticks_once()
    publisher.poll_bars_once()
    worker.poll_once()
    with factory() as session:
        orders = session.scalars(select(ExecutionOrder)).all()
        assert len(orders) == 1
        order_id = orders[0].id
        assert orders[0].status == "unknown"  # actual HTTP submit timed out, no fabricated fill
        assert session.scalars(select(ExecutionDecision)).first() is not None

    # Let pending broker work reach its visibility boundary, then restart the
    # ordinary worker and reconcile by intent; never issue another submit.
    _wait_until(lambda: len(simulator.broker().deals(simulator.status().canonical_cursor_us, 0, 2**62)) == 1)
    worker._shutdown()
    restarted_components = q_execution._build_components(MarketDataService(), settings=components.settings)
    restarted = q_execution._build_worker(restarted_components, poll_interval_seconds=0)
    try:
        with factory() as session:
            recovered = restarted.recovery.recover(session, worker_id=components.settings.execution_worker_id)
            assert not recovered.failed_closed
            session.commit()
        restarted.poll_once()
        with factory() as session:
            order = session.get(ExecutionOrder, order_id)
            assert order.status == "filled"
            fills = session.scalars(select(ExecutionFill)).all()
            assert len(fills) == 1 and fills[0].fee == Decimal("2")
            fees = session.scalars(
                select(ExecutionLedgerEntry).where(ExecutionLedgerEntry.fill_id == fills[0].id)
            ).all()
            assert any(entry.amount == Decimal("-2") for entry in fees)
            assert {"decisions", "orders", "fills", "ledger"} <= set(session.scalars(select(OutboxEvent.topic)))
        assert OutboxRelay(factory, redis_client).run_once() > 0
        for topic in ("quotes", "bars.forming", "bars.completed", "decisions", "orders", "fills", "ledger"):
            assert redis_client.xlen(stream_key(topic)) > 0, topic
        fill_response = http.get(f"/api/v1/execution/deployments/{deployment['id']}/fills")
        assert fill_response.status_code == 200
        assert len(fill_response.json()["items"]) == 1
        assert Decimal(str(fill_response.json()["items"][0]["fee"])) == Decimal("2")
        fill_events = redis_client.xrange(stream_key("fills"))
        assert len(fill_events) == 1
        fill_event, _ = decode_entry(fill_events[0][1])
        assert Decimal(str(fill_event.payload["fee"])) == Decimal("2")
        restarted.poll_once()
        with factory() as session:
            assert len(session.scalars(select(ExecutionFill)).all()) == 1
            assert len(session.scalars(select(ExecutionOrder)).all()) == 1
        deals = simulator.broker().deals(simulator.status().canonical_cursor_us, 0, 2**62)
        assert len(deals) == 1
    finally:
        restarted._shutdown()
