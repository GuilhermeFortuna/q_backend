"""EdgeBroker submit/lookup and live-gate tests."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from q_backend.execution.brokers.base import (
    BrokerOrderLookupStatus,
    BrokerRejectionCode,
    BrokerSubmissionOutcome,
    MarketOrderRequest,
    PaperCostConfig,
)
from q_backend.execution.brokers.edge import EdgeBroker
from q_backend.execution.brokers.fakes import FixedClock
from q_backend.execution.brokers.live_gates import LiveExecutionGates, evaluate_live_gates
from q_backend.execution.domain import BrokerMode, ExecutionSide
from q_backend.execution.edge_client import EdgeClient, EdgeTimeouts
from tests.execution.fake_edge import fake_edge_server, make_filled_lookup_deal


@pytest.fixture
def cost_config() -> PaperCostConfig:
    return PaperCostConfig(point_value=Decimal("0.2"))


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock(datetime(2024, 6, 1, 15, 0, tzinfo=timezone.utc))


def _open_gates(*, account: int = 12345678) -> LiveExecutionGates:
    return LiveExecutionGates(
        enabled=True,
        account_allowlist=frozenset({account}),
        deployment_live_activation_enabled=True,
        controlled_account_validated=True,
    )


def _request(
    order_id: UUID,
    *,
    live_activation_enabled: bool = True,
) -> MarketOrderRequest:
    return MarketOrderRequest(
        deployment_id=UUID("11111111-1111-1111-1111-111111111111"),
        order_id=order_id,
        symbol="WIN$",
        side=ExecutionSide.BUY,
        quantity=Decimal("1"),
        external_fill_id=f"mt5_live:{order_id}",
        broker_mode=BrokerMode.MT5_LIVE,
        intent_created_at=datetime(2024, 6, 1, 14, 59, tzinfo=timezone.utc),
        live_activation_enabled=live_activation_enabled,
    )


def _broker(base_url: str, clock: FixedClock, gates: LiveExecutionGates | None = None) -> EdgeBroker:
    client = EdgeClient(base_url, timeouts=EdgeTimeouts())
    return EdgeBroker(
        client=client,
        clock=clock,
        gates=gates or _open_gates(),
    )


@pytest.mark.parametrize(
    "gates,account_login,expected_code",
    [
        (LiveExecutionGates(enabled=False), 12345678, BrokerRejectionCode.LIVE_LOCKED),
        (
            LiveExecutionGates(
                enabled=True,
                account_allowlist=frozenset({999}),
                deployment_live_activation_enabled=True,
                controlled_account_validated=True,
            ),
            12345678,
            BrokerRejectionCode.ACCOUNT_NOT_ALLOWLISTED,
        ),
        (
            LiveExecutionGates(
                enabled=True,
                account_allowlist=frozenset({12345678}),
                deployment_live_activation_enabled=False,
                controlled_account_validated=True,
            ),
            12345678,
            BrokerRejectionCode.LIVE_LOCKED,
        ),
        (
            LiveExecutionGates(
                enabled=True,
                account_allowlist=frozenset({12345678}),
                deployment_live_activation_enabled=True,
                controlled_account_validated=False,
            ),
            12345678,
            BrokerRejectionCode.LIVE_LOCKED,
        ),
    ],
)
def test_activation_gate_matrix(gates, account_login, expected_code):
    rejection = evaluate_live_gates(gates, account_login=account_login)
    assert rejection is not None
    assert rejection.code == expected_code


def test_accepted_then_filled(clock, cost_config):
    order_id = uuid4()
    with fake_edge_server() as (base_url, state):
        state.lookup_handlers[str(order_id)] = lambda _body: {
            "outcome": "filled",
            "closes_intent": True,
            "deals": [
                make_filled_lookup_deal(
                    ticket=7001,
                    symbol="WIN$",
                    volume=1.0,
                    price=130010.0,
                )
            ],
        }
        broker = _broker(base_url, clock)
        result = broker.submit_market_order(_request(order_id), cost_config=cost_config)
        assert result.outcome == BrokerSubmissionOutcome.FILLED
        submits = [r for r in state.requests if r[1] == "/v1/submit"]
        assert len(submits) == 1


def test_accepted_then_not_yet_filled_is_unknown(clock, cost_config):
    order_id = uuid4()
    with fake_edge_server() as (base_url, state):
        state.default_lookup = {"outcome": "not_found", "closes_intent": True}
        broker = _broker(base_url, clock)
        result = broker.submit_market_order(_request(order_id), cost_config=cost_config)
        assert result.outcome == BrokerSubmissionOutcome.UNKNOWN


def test_rejected_submission(clock, cost_config):
    order_id = uuid4()
    with fake_edge_server() as (base_url, state):
        state.default_submit = {
            "outcome": "rejected",
            "retcode": 10006,
            "reason": "order_send rejected: reject",
        }
        broker = _broker(base_url, clock)
        result = broker.submit_market_order(_request(order_id), cost_config=cost_config)
        assert result.outcome == BrokerSubmissionOutcome.REJECTED


def test_indeterminate_submission(clock, cost_config):
    order_id = uuid4()
    with fake_edge_server() as (base_url, state):
        state.default_submit = {"outcome": "indeterminate", "reason": "timeout"}
        broker = _broker(base_url, clock)
        result = broker.submit_market_order(_request(order_id), cost_config=cost_config)
        assert result.outcome == BrokerSubmissionOutcome.UNKNOWN


def test_gate_closed_rejects_without_submit(clock, cost_config):
    order_id = uuid4()
    with fake_edge_server() as (base_url, state):
        broker = _broker(base_url, clock, gates=LiveExecutionGates())
        result = broker.submit_market_order(_request(order_id), cost_config=cost_config)
        assert result.outcome == BrokerSubmissionOutcome.REJECTED
        assert result.rejection.code == BrokerRejectionCode.LIVE_LOCKED
        assert not any(r[1] == "/v1/submit" for r in state.requests)


def test_quote_age_rejects_stale_edge_quote(clock):
    from q_backend.execution.quote_source import EdgeQuoteSource

    with fake_edge_server() as (base_url, state):
        state.quotes["WIN$"] = {
            "symbol": "WIN$",
            "bid": 130000.0,
            "ask": 130010.0,
            "last": 130005.0,
            "time_msc": int(clock.now().timestamp() * 1000),
            "age_ms": 120_000,
        }
        client = EdgeClient(base_url, timeouts=EdgeTimeouts())
        quote_source = EdgeQuoteSource(client=client, clock=clock.now)
        quote = quote_source.get_quote("WIN$")
        assert quote is not None
        from q_backend.execution.brokers.paper import validate_quote

        rejection = validate_quote(quote, now=clock.now(), max_age_seconds=30.0)
        assert rejection is not None
        assert rejection.code == BrokerRejectionCode.STALE_QUOTE


def test_lookup_reconciliation_states(clock, cost_config):
    order_id = uuid4()
    request = _request(order_id)
    with fake_edge_server() as (base_url, state):
        broker = _broker(base_url, clock)

        state.default_lookup = {
            "outcome": "filled",
            "closes_intent": True,
            "deals": [make_filled_lookup_deal(ticket=1, symbol="WIN$", volume=1.0, price=100.0)],
        }
        assert broker.lookup_order(request).status == BrokerOrderLookupStatus.FILLED

        state.default_lookup = {"outcome": "rejected", "retcode": 10006, "closes_intent": True}
        assert broker.lookup_order(request).status == BrokerOrderLookupStatus.REJECTED

        state.default_lookup = {"outcome": "not_found", "closes_intent": True}
        assert broker.lookup_order(request).status == BrokerOrderLookupStatus.NOT_FOUND

        state.default_lookup = {
            "outcome": "unavailable",
            "reason": "terminal down",
            "closes_intent": False,
        }
        assert broker.lookup_order(request).status == BrokerOrderLookupStatus.UNAVAILABLE
