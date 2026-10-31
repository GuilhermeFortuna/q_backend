"""Broker edge over loopback HTTP, driven by the real EdgeClient and a real-time driver."""

from __future__ import annotations

import time
import uuid
from datetime import UTC, timedelta

import pytest

from q_backend.execution.edge_client import EdgeClient, EdgeTimeouts
from q_backend.replay.edge import create_edge_app
from q_backend.replay.gateway import ServerThread
from q_backend.replay.journal import ReplayRunStore
from q_backend.replay.service import Driver, ReplaySimulator

from .support import BASE, QUOTE, TRADE, config_for, ms, spec, write_dataset

SYMBOL = "WIN$N"
LATENCY = {"order_transport_ms": 50, "broker_accept_ms": 50, "broker_execution_ms": 100, "broker_response_ms": 100}


@pytest.fixture
def live(tmp_path):
    # A constant book (ask 100) every 50 ms: the fill price does not depend on wall-clock jitter.
    ticks = [(ms(-1) + step * 50, 99.0, 100.0, 0.0, 0.0, QUOTE) for step in range(0, 1400)]
    ticks.append((ms(0), 99.0, 100.0, 100.0, 1.0, TRADE))
    write_dataset(tmp_path, {SYMBOL: ticks}, {SYMBOL: spec()})
    store = ReplayRunStore(tmp_path / "replay.sqlite3")
    simulator = ReplaySimulator(store, dataset_root=tmp_path)
    simulator.create(config_for(latency=LATENCY, end_seconds=600), run_id="live-1", generation="gen-1")
    simulator.command(command_id=str(uuid.uuid4()), expected_revision=0, action="resume")
    driver = Driver(simulator, interval_s=0.002)
    driver.start()
    server = ServerThread(create_edge_app(simulator))
    url = server.start()
    client = EdgeClient(url, timeouts=EdgeTimeouts(connect_s=2.0, read_s=10.0, submit_read_s=10.0))
    yield simulator, client
    driver.stop()
    server.stop()


def _market_order(volume: float = 1.0) -> dict:
    return {"symbol": SYMBOL, "side": "buy", "volume": volume}


def test_submit_answers_at_visibility_while_market_keeps_moving(live) -> None:
    simulator, client = live
    before = simulator.status().canonical_cursor_us
    started = time.monotonic()
    intent_id = str(uuid.uuid4())
    reply = client._submit_body(intent_id, {"intent_id": intent_id, "order": _market_order()})  # noqa: SLF001
    elapsed = time.monotonic() - started
    assert reply["outcome"] == "accepted" and reply["retcode"] == 10009
    # Transport 50 + accept 50 + execution 100 + response 100 ms of market time at 1x.
    assert elapsed >= 0.28
    advanced = (simulator.status().canonical_cursor_us - before) / 1_000_000
    assert advanced >= 0.28


def test_repeated_intent_is_refused_without_a_second_order(live) -> None:
    simulator, client = live
    intent_id = str(uuid.uuid4())
    first = client._submit_body(intent_id, {"intent_id": intent_id, "order": _market_order()})  # noqa: SLF001
    again = client._submit_body(intent_id, {"intent_id": intent_id, "order": _market_order()})  # noqa: SLF001
    assert first["outcome"] == "accepted"
    assert again["outcome"] == "indeterminate" and "already received" in again["reason"]
    assert len(simulator.broker().deals(simulator.status().canonical_cursor_us, 0, 2**62)) == 1


def test_reads_and_lookup_reflect_the_fill(live) -> None:
    simulator, client = live
    intent_id = str(uuid.uuid4())
    client._submit_body(intent_id, {"intent_id": intent_id, "order": _market_order(2.0)})  # noqa: SLF001
    account = client.account()
    assert account.trade_mode == "demo" and account.balance < 100000.0 + 1
    positions = client.positions(SYMBOL)
    assert len(positions) == 1 and positions[0]["volume"] == 2.0
    # Deal and lookup windows are market time, which starts at the run's interval start.
    now = BASE.astimezone(UTC)
    deals = client.deals(now - timedelta(hours=1), now + timedelta(hours=1), symbol=SYMBOL)
    assert len(deals) == 1 and deals[0]["price"] == 100.0
    found = client.lookup(uuid.UUID(intent_id), now - timedelta(hours=1), now + timedelta(hours=1))
    assert found["outcome"] == "filled" and found["closes_intent"] is True


def test_health_matches_the_edge_contract(live) -> None:
    _, client = live
    health = client.health()
    assert health.status == "ok" and health.schema_version == "1.2" and health.mt5_connected is True
