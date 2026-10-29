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
from q_backend.execution.brokers.edge import EdgeBroker, MT5OrdersAccount, mt5_order_status
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


def _gates_for(*logins: int, validated: bool = False, deployment: bool = True) -> LiveExecutionGates:
    return LiveExecutionGates(
        enabled=True,
        account_allowlist=frozenset(logins),
        deployment_live_activation_enabled=deployment,
        controlled_account_validated=validated,
    )


def _filled_lookup(order_id: UUID):
    return lambda _body: {
        "outcome": "filled",
        "closes_intent": True,
        "deals": [make_filled_lookup_deal(ticket=7001, symbol="WIN$", volume=1.0, price=130010.0)],
    }


def _set_trade_mode(state, trade_mode: str | None) -> None:
    if trade_mode is None:
        state.account.pop("trade_mode", None)
    else:
        state.account["trade_mode"] = trade_mode


@pytest.mark.parametrize(
    "trade_mode,validated,rejected",
    [
        ("demo", False, False),
        ("contest", False, True),
        ("real", False, True),
        (None, False, True),
        ("real", True, False),
        (None, True, False),
    ],
)
def test_controlled_account_gate_by_trade_mode(trade_mode, validated, rejected):
    rejection = evaluate_live_gates(
        _gates_for(12345678, validated=validated),
        account_login=12345678,
        trade_mode=trade_mode,
    )
    assert (rejection is not None) == rejected
    if rejected:
        assert rejection.code == BrokerRejectionCode.LIVE_LOCKED


def test_demo_account_order_passes_without_validation(clock, cost_config):
    order_id = uuid4()
    with fake_edge_server() as (base_url, state):
        _set_trade_mode(state, "demo")
        state.lookup_handlers[str(order_id)] = _filled_lookup(order_id)
        broker = _broker(base_url, clock, gates=_gates_for(12345678))
        result = broker.submit_market_order(_request(order_id), cost_config=cost_config)
        assert result.outcome == BrokerSubmissionOutcome.FILLED
        assert len([r for r in state.requests if r[1] == "/v1/submit"]) == 1


@pytest.mark.parametrize(
    "trade_mode,expected_message",
    [
        ("contest", "trade mode is contest"),
        ("real", "trade mode is real"),
        (None, "reports no trade mode"),
    ],
)
def test_unvalidated_non_demo_order_is_rejected_without_submit(clock, cost_config, trade_mode, expected_message):
    order_id = uuid4()
    with fake_edge_server() as (base_url, state):
        _set_trade_mode(state, trade_mode)
        broker = _broker(base_url, clock, gates=_gates_for(12345678))
        result = broker.submit_market_order(_request(order_id), cost_config=cost_config)
        assert result.outcome == BrokerSubmissionOutcome.REJECTED
        assert result.rejection.code == BrokerRejectionCode.LIVE_LOCKED
        assert expected_message in result.rejection.message
        assert [r for r in state.requests if r[1] == "/v1/submit"] == []


def test_validated_real_account_order_reaches_edge(clock, cost_config):
    order_id = uuid4()
    with fake_edge_server() as (base_url, state):
        _set_trade_mode(state, "real")
        state.lookup_handlers[str(order_id)] = _filled_lookup(order_id)
        broker = _broker(base_url, clock, gates=_gates_for(12345678, validated=True))
        result = broker.submit_market_order(_request(order_id), cost_config=cost_config)
        assert result.outcome == BrokerSubmissionOutcome.FILLED
        assert len([r for r in state.requests if r[1] == "/v1/submit"]) == 1


def test_switch_from_demo_to_real_between_submissions_is_rejected(clock, cost_config):
    first_id = uuid4()
    with fake_edge_server() as (base_url, state):
        _set_trade_mode(state, "demo")
        state.lookup_handlers[str(first_id)] = _filled_lookup(first_id)
        broker = _broker(base_url, clock, gates=_gates_for(12345678, 22222222))
        first = broker.submit_market_order(_request(first_id), cost_config=cost_config)
        assert first.outcome == BrokerSubmissionOutcome.FILLED

        state.account = {**state.account, "login": 22222222, "trade_mode": "real"}
        second = broker.submit_market_order(_request(uuid4()), cost_config=cost_config)

        assert second.outcome == BrokerSubmissionOutcome.REJECTED
        assert second.rejection.code == BrokerRejectionCode.LIVE_LOCKED
        assert len([r for r in state.requests if r[1] == "/v1/submit"]) == 1


def test_mt5_order_status_available_for_demo_account_without_deployment_flag():
    with fake_edge_server() as (base_url, state):
        _set_trade_mode(state, "demo")
        status = mt5_order_status(
            EdgeClient(base_url, timeouts=EdgeTimeouts()),
            _gates_for(12345678, deployment=False),
        )
    assert status.capability == "available"
    assert status.reason is None
    assert status.account == MT5OrdersAccount(login=12345678, server="Broker-Demo", trade_mode="demo")


def test_mt5_order_status_validation_required_for_real_account():
    with fake_edge_server() as (base_url, state):
        _set_trade_mode(state, "real")
        status = mt5_order_status(EdgeClient(base_url, timeouts=EdgeTimeouts()), _gates_for(12345678))
    assert status.capability == "validation_required"
    assert "trade mode is real" in status.reason
    assert status.account.trade_mode == "real"


def test_mt5_order_status_locked_when_gate_closed_reports_account():
    with fake_edge_server() as (base_url, state):
        _set_trade_mode(state, "demo")
        status = mt5_order_status(EdgeClient(base_url, timeouts=EdgeTimeouts()), LiveExecutionGates())
    assert status.capability == "locked"
    assert "Q_LIVE_EXECUTION_ENABLED" in status.reason
    assert status.account == MT5OrdersAccount(login=12345678, server="Broker-Demo", trade_mode="demo")


def test_mt5_order_status_locked_when_account_not_allowlisted():
    with fake_edge_server() as (base_url, state):
        _set_trade_mode(state, "demo")
        status = mt5_order_status(EdgeClient(base_url, timeouts=EdgeTimeouts()), _gates_for(999))
    assert status.capability == "locked"
    assert "allowlist" in status.reason


def test_mt5_order_status_locked_when_edge_unreachable():
    client = EdgeClient("http://127.0.0.1:9", timeouts=EdgeTimeouts(connect_s=0.5, read_s=0.5))
    status = mt5_order_status(client, _gates_for(12345678))
    assert status.capability == "locked"
    assert status.reason
    assert status.account is None
