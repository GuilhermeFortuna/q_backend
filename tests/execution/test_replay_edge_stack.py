"""Normal stack components against the replay broker edge (Q-145 step 8).

The real EdgeClient and EdgeBroker submit a market order to the simulator's broker edge over
loopback while the simulator's real-time driver moves the market. The fill, its price and the
lookup outcome come from the simulated broker, not from a test double.
"""

from __future__ import annotations

import uuid
from datetime import timezone
from decimal import Decimal

import pytest

from q_backend.execution.brokers.base import MarketOrderRequest, PaperCostConfig
from q_backend.execution.brokers.edge import EdgeBroker
from q_backend.execution.brokers.fakes import FixedClock
from q_backend.execution.brokers.live_gates import LiveExecutionGates
from q_backend.execution.domain import BrokerMode, ExecutionSide
from q_backend.execution.edge_client import EdgeClient, EdgeTimeouts
from q_backend.replay.edge import create_edge_app
from q_backend.replay.gateway import ServerThread
from q_backend.replay.journal import ReplayRunStore
from q_backend.replay.service import Driver, ReplaySimulator
from tests.replay.support import BASE, QUOTE, config_for, ms, spec, write_dataset

SYMBOL = "WIN$N"
LOGIN = 900001


@pytest.fixture
def replay_edge(tmp_path):
    book = [(ms(-1) + step * 50, 99.0, 100.0, 0.0, 0.0, QUOTE) for step in range(0, 2600)]
    write_dataset(tmp_path, {SYMBOL: book}, {SYMBOL: spec()})
    store = ReplayRunStore(tmp_path / "stack.sqlite3")
    simulator = ReplaySimulator(store, dataset_root=tmp_path)
    latency = {"order_transport_ms": 50, "broker_accept_ms": 50, "broker_execution_ms": 100, "broker_response_ms": 100}
    simulator.create(config_for(latency=latency, end_seconds=600), run_id="stack-1", generation="gen-stack")
    simulator.command(command_id=str(uuid.uuid4()), expected_revision=0, action="resume")
    driver = Driver(simulator, interval_s=0.002)
    driver.start()
    server = ServerThread(create_edge_app(simulator))
    url = server.start()
    yield simulator, url
    driver.stop()
    server.stop()
    store.close()


def _broker(url: str) -> EdgeBroker:
    client = EdgeClient(url, timeouts=EdgeTimeouts(connect_s=2.0, read_s=10.0, submit_read_s=10.0))
    return EdgeBroker(
        client=client,
        clock=FixedClock(BASE.replace(tzinfo=timezone.utc)),
        gates=LiveExecutionGates(
            enabled=True,
            account_allowlist=frozenset({LOGIN}),
            deployment_live_activation_enabled=True,
            controlled_account_validated=False,  # the demo account passes validation on its own
        ),
    )


def test_market_order_fills_through_the_replay_edge(replay_edge) -> None:
    simulator, url = replay_edge
    before = simulator.status().canonical_cursor_us
    broker = _broker(url)
    request = MarketOrderRequest(
        deployment_id=uuid.uuid4(),
        order_id=uuid.uuid4(),
        symbol=SYMBOL,
        side=ExecutionSide.BUY,
        quantity=Decimal("1"),
        external_fill_id="fill-1",
        broker_mode=BrokerMode.MT5_LIVE,
        intent_created_at=BASE.replace(tzinfo=timezone.utc),
        live_activation_enabled=True,
    )
    cost = PaperCostConfig(point_value=Decimal("1"), slippage_points=Decimal("0"), max_quote_age_seconds=60.0)
    result = broker.submit_market_order(request, cost_config=cost)
    assert result.outcome.value == "filled", result.rejection
    assert result.fill is not None and result.fill.price == Decimal("100")
    # The fill waited for the modeled latency: at least transport + accept + execution + response.
    elapsed_market_ms = (simulator.status().canonical_cursor_us - before) / 1000
    assert elapsed_market_ms >= 300
    assert len(result.external_deal_ids) == 1
