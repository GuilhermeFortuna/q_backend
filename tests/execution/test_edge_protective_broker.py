"""EdgeBroker pending, protective, cancel and read-only lookups against the HTTP edge fake (edge 1.2)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from q_backend.execution.brokers.base import (
    BrokerOrderLookupStatus,
    BrokerRejectionCode,
    BrokerSubmissionOutcome,
    CancelOrderRequest,
    MarketOrderRequest,
    PaperCostConfig,
    PendingOrderRequest,
    PositionExitRequest,
    ProtectionRequest,
)
from q_backend.execution.brokers.edge import EdgeBroker
from q_backend.execution.brokers.fakes import FixedClock
from q_backend.execution.brokers.live_gates import LiveExecutionGates
from q_backend.execution.domain import BrokerMode, ExecutionOrderType, ExecutionSide
from q_backend.execution.edge_client import EdgeClient, EdgeTimeouts
from tests.execution.fake_edge import FakeEdgeState, fake_edge_server

DEPLOYMENT = UUID("11111111-1111-1111-1111-111111111111")
NOW = datetime(2026, 10, 30, 14, 0, tzinfo=timezone.utc)


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock(NOW)


def _gates() -> LiveExecutionGates:
    return LiveExecutionGates(
        enabled=True,
        account_allowlist=frozenset({12345678}),
        deployment_live_activation_enabled=True,
        controlled_account_validated=True,
    )


def _broker(base_url: str, clock: FixedClock) -> EdgeBroker:
    return EdgeBroker(client=EdgeClient(base_url, timeouts=EdgeTimeouts()), clock=clock, gates=_gates())


def _edge_state(**overrides) -> FakeEdgeState:
    return FakeEdgeState(**{"schema_version": "1.2", **overrides})


def _pending(order_id: UUID | None = None, **overrides) -> PendingOrderRequest:
    values = dict(
        deployment_id=DEPLOYMENT,
        order_id=order_id or uuid4(),
        symbol="WIN$",
        side=ExecutionSide.BUY,
        order_type=ExecutionOrderType.STOP,
        quantity=Decimal("1"),
        price=Decimal("130100"),
        stop_loss=Decimal("129900"),
        take_profit=Decimal("130400"),
        expires_at=NOW + timedelta(hours=1),
        intent_created_at=NOW - timedelta(seconds=5),
        broker_mode=BrokerMode.MT5_LIVE,
        live_activation_enabled=True,
    )
    values.update(overrides)
    return PendingOrderRequest(**values)


def _submit_bodies(state: FakeEdgeState) -> list[dict]:
    return [body for method, path, body in state.requests if path == "/v1/submit" and body is not None]


def test_pending_stop_submission_sends_pending_action_and_reports_working(clock):
    state = _edge_state()
    request = _pending()
    state.submit_handlers[str(request.order_id)] = lambda body: {
        "outcome": "accepted",
        "order_ticket": 223344,
        "retcode": 10009,
    }
    with fake_edge_server(state) as (base, state):
        result = _broker(base, clock).submit_pending_order(request)
    assert result.outcome == BrokerSubmissionOutcome.WORKING
    assert result.external_order_id == "223344"
    body = _submit_bodies(state)[-1]
    assert body["action"] == "pending"
    assert body["order"]["type"] == 4
    assert body["order"]["price"] == 130100.0
    assert body["order"]["sl"] == 129900.0
    assert body["order"]["tp"] == 130400.0
    assert body["order"]["expiration"] == int(request.expires_at.timestamp())


def test_pending_limit_without_expiration_is_good_till_cancelled(clock):
    state = _edge_state()
    request = _pending(
        order_type=ExecutionOrderType.LIMIT, side=ExecutionSide.SELL, expires_at=None, stop_loss=None, take_profit=None
    )
    with fake_edge_server(state) as (base, state):
        result = _broker(base, clock).submit_pending_order(request)
    assert result.outcome == BrokerSubmissionOutcome.WORKING
    body = _submit_bodies(state)[-1]
    assert body["order"]["type"] == 3
    assert "expiration" not in body["order"]


def test_pending_rejection_is_rejected_and_indeterminate_is_unknown(clock):
    state = _edge_state()
    rejected = _pending()
    unknown = _pending()
    state.submit_handlers[str(rejected.order_id)] = lambda body: {
        "outcome": "rejected",
        "retcode": 10016,
        "reason": "invalid stops",
    }
    state.submit_handlers[str(unknown.order_id)] = lambda body: {"outcome": "indeterminate", "reason": "timeout"}
    with fake_edge_server(state) as (base, state):
        broker = _broker(base, clock)
        assert broker.submit_pending_order(rejected).outcome == BrokerSubmissionOutcome.REJECTED
        assert broker.submit_pending_order(unknown).outcome == BrokerSubmissionOutcome.UNKNOWN


def test_pending_refused_below_edge_1_2_without_sending_an_order(clock):
    state = _edge_state(schema_version="1.1")
    with fake_edge_server(state) as (base, state):
        result = _broker(base, clock).submit_pending_order(_pending())
    assert result.outcome == BrokerSubmissionOutcome.REJECTED
    assert result.rejection.code == BrokerRejectionCode.UNSUPPORTED_ORDER_KIND
    assert _submit_bodies(state) == []


def test_pending_lookup_maps_placed_filled_and_terminal_outcomes(clock):
    state = _edge_state()
    placed = _pending()
    filled = _pending()
    cancelled = _pending()
    expired = _pending()
    state.lookup_handlers[str(placed.order_id)] = lambda body: {
        "outcome": "placed",
        "order_ticket": 77,
        "closes_intent": False,
    }
    state.lookup_handlers[str(filled.order_id)] = lambda body: {
        "outcome": "filled",
        "closes_intent": True,
        "deals": [
            {
                "ticket": 5001,
                "order_ticket": 77,
                "symbol": "WIN$",
                "type": 0,
                "volume": 1.0,
                "price": 130105.0,
                "position_id": 42,
                "reason": "client",
                "time_msc": int(NOW.timestamp() * 1000),
            }
        ],
    }
    state.lookup_handlers[str(cancelled.order_id)] = lambda body: {"outcome": "cancelled", "closes_intent": True}
    state.lookup_handlers[str(expired.order_id)] = lambda body: {"outcome": "expired", "closes_intent": True}
    with fake_edge_server(state) as (base, state):
        broker = _broker(base, clock)
        placed_state = broker.lookup_pending_order(placed)
        filled_state = broker.lookup_pending_order(filled)
        cancelled_state = broker.lookup_pending_order(cancelled)
        expired_state = broker.lookup_pending_order(expired)
    assert placed_state.status == BrokerOrderLookupStatus.WORKING
    assert placed_state.external_order_id == "77"
    assert filled_state.status == BrokerOrderLookupStatus.FILLED
    assert filled_state.external_position_id == "42"
    assert filled_state.fill is not None and filled_state.fill.quantity == Decimal("1")
    assert cancelled_state.status == BrokerOrderLookupStatus.CANCELLED
    assert expired_state.status == BrokerOrderLookupStatus.EXPIRED
    lookups = [body for method, path, body in state.requests if path == "/v1/lookup"]
    assert all(body["kind"] == "pending" for body in lookups)


def test_protection_sends_sltp_and_reports_applied(clock):
    state = _edge_state()
    request = ProtectionRequest(
        deployment_id=DEPLOYMENT,
        order_id=uuid4(),
        symbol="WIN$",
        position_ticket="42",
        stop_loss=Decimal("129950"),
        take_profit=None,
        intent_created_at=NOW,
        broker_mode=BrokerMode.MT5_LIVE,
        live_activation_enabled=True,
    )
    with fake_edge_server(state) as (base, state):
        result = _broker(base, clock).set_protection(request)
    assert result.outcome == BrokerSubmissionOutcome.APPLIED
    assert result.external_position_id == "42"
    body = _submit_bodies(state)[-1]
    assert body["action"] == "sltp"
    assert body["position"] == 42
    assert body["sl"] == 129950.0
    assert body["tp"] == 0.0


def test_cancel_sends_remove_and_reports_cancelled(clock):
    state = _edge_state()
    request = CancelOrderRequest(
        deployment_id=DEPLOYMENT,
        order_id=uuid4(),
        symbol="WIN$",
        external_order_id="77",
        intent_created_at=NOW,
        broker_mode=BrokerMode.MT5_LIVE,
        live_activation_enabled=True,
    )
    with fake_edge_server(state) as (base, state):
        result = _broker(base, clock).cancel_order(request)
    assert result.outcome == BrokerSubmissionOutcome.CANCELLED
    body = _submit_bodies(state)[-1]
    assert body == {**body, "action": "remove", "order_ticket": 77, "symbol": "WIN$"}


def test_market_order_with_levels_sends_an_explicit_deal_action(clock):
    state = _edge_state()
    request = MarketOrderRequest(
        deployment_id=DEPLOYMENT,
        order_id=uuid4(),
        symbol="WIN$",
        side=ExecutionSide.BUY,
        quantity=Decimal("1"),
        external_fill_id="mt5_live:x",
        broker_mode=BrokerMode.MT5_LIVE,
        intent_created_at=NOW,
        live_activation_enabled=True,
        stop_loss=Decimal("129900"),
        take_profit=Decimal("130300"),
    )
    with fake_edge_server(state) as (base, state):
        _broker(base, clock).submit_market_order(request, cost_config=PaperCostConfig(point_value=Decimal("0.2")))
    body = _submit_bodies(state)[-1]
    assert body["action"] == "deal"
    assert body["order"]["sl"] == 129900.0
    assert body["order"]["tp"] == 130300.0


def test_protection_lookup_checks_the_levels_on_the_position(clock):
    state = _edge_state()
    state.positions = [
        {"ticket": 42, "symbol": "WIN$", "type": 0, "volume": 1.0, "price_open": 130100.0, "sl": 129950.0, "tp": 0.0}
    ]
    request = ProtectionRequest(
        deployment_id=DEPLOYMENT,
        order_id=uuid4(),
        symbol="WIN$",
        position_ticket="42",
        stop_loss=Decimal("129950"),
        take_profit=None,
        intent_created_at=NOW,
        broker_mode=BrokerMode.MT5_LIVE,
    )
    with fake_edge_server(state) as (base, state):
        broker = _broker(base, clock)
        applied = broker.lookup_protection(request)
        other = broker.lookup_protection(request.model_copy(update={"stop_loss": Decimal("129000")}))
        gone = broker.lookup_protection(request.model_copy(update={"position_ticket": "99"}))
    assert applied.status == BrokerOrderLookupStatus.FILLED
    assert other.status == BrokerOrderLookupStatus.NOT_FOUND
    assert gone.status == BrokerOrderLookupStatus.UNAVAILABLE


def test_cancellation_lookup_reports_whether_the_order_still_rests(clock):
    state = _edge_state()
    state.working_orders = [
        {"ticket": 77, "symbol": "WIN$", "type": 4, "volume": 1.0, "price": 130100.0, "expiration": None}
    ]
    request = CancelOrderRequest(
        deployment_id=DEPLOYMENT,
        order_id=uuid4(),
        symbol="WIN$",
        external_order_id="77",
        intent_created_at=NOW,
        broker_mode=BrokerMode.MT5_LIVE,
    )
    with fake_edge_server(state) as (base, state):
        broker = _broker(base, clock)
        resting = broker.lookup_cancellation(request)
        removed = broker.lookup_cancellation(request.model_copy(update={"external_order_id": "78"}))
    assert resting.status == BrokerOrderLookupStatus.NOT_FOUND
    assert removed.status == BrokerOrderLookupStatus.CANCELLED


def test_position_exit_is_reported_as_a_stop_loss_fill_with_its_reason(clock):
    state = _edge_state()
    state.positions = []
    exit_time_msc = int((NOW + timedelta(minutes=30)).timestamp() * 1000)
    state.deals = [
        {
            "ticket": 6001,
            "order_ticket": 5555,
            "symbol": "WIN$",
            "type": 1,
            "volume": 1.0,
            "price": 129900.0,
            "position_id": 42,
            "reason": "sl",
            "time_msc": exit_time_msc,
        }
    ]
    request = PositionExitRequest(
        deployment_id=DEPLOYMENT,
        symbol="WIN$",
        position_ticket="42",
        opened_at=NOW,
        broker_mode=BrokerMode.MT5_LIVE,
    )
    with fake_edge_server(state) as (base, state):
        result = _broker(base, clock).lookup_position_exit(request)
    assert result.status == BrokerOrderLookupStatus.FILLED
    assert result.fill is not None
    assert result.fill.side == ExecutionSide.SELL
    assert result.fill.quantity == Decimal("1")
    assert result.fill.metadata["exit_reason"] == "stop_loss"


def test_position_still_open_is_not_an_exit(clock):
    state = _edge_state()
    state.positions = [{"ticket": 42, "symbol": "WIN$", "type": 0, "volume": 1.0, "price_open": 130100.0}]
    request = PositionExitRequest(
        deployment_id=DEPLOYMENT,
        symbol="WIN$",
        position_ticket="42",
        opened_at=NOW,
        broker_mode=BrokerMode.MT5_LIVE,
    )
    with fake_edge_server(state) as (base, state):
        result = _broker(base, clock).lookup_position_exit(request)
    assert result.status == BrokerOrderLookupStatus.NOT_FOUND
