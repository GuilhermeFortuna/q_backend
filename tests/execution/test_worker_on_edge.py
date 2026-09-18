"""Worker integration tests against the fake execution edge."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

from q_backend.execution.brokers.base import MarketOrderRequest, PaperCostConfig
from q_backend.execution.brokers.edge import EdgeBroker
from q_backend.execution.brokers.fakes import FixedClock
from q_backend.execution.brokers.live_gates import LiveExecutionGates
from q_backend.execution.brokers.paper import PaperBroker
from q_backend.execution.brokers.routing import BrokerRouter
from q_backend.execution.domain import (
    BrokerMode,
    DecisionOutcome,
    DeploymentLifecycle,
    ExecutionOrderStatus,
    ExecutionSide,
    ReconciliationState,
    SignalAction,
)
from q_backend.execution.edge_client import EdgeClient, EdgeTimeouts
from q_backend.execution.ledger import ExecutionLedger
from q_backend.execution.quote_source import EdgeQuoteSource
from q_backend.execution.reconciliation import OrderReconciler
from q_backend.execution.results import EvaluationPhaseTiming, ForwardDecisionResult
from q_backend.execution.service import ExecutionService
from q_backend.storage.db.execution_repositories import (
    acquire_worker_lease,
    create_execution_deployment,
    create_execution_order_intent,
    create_paper_account,
    list_orders_for_deployment,
    transition_execution_order,
)
from tests.execution.conftest import strategy_identity
from tests.execution.fake_edge import fake_edge_server, make_filled_lookup_deal


def _components(base_url: str, clock: FixedClock):
    client = EdgeClient(base_url, timeouts=EdgeTimeouts())
    quote_source = EdgeQuoteSource(client=client, clock=clock.now)
    paper = PaperBroker(quote_source=quote_source, clock=clock)
    live = EdgeBroker(
        client=client,
        clock=clock,
        gates=LiveExecutionGates(
            enabled=True,
            account_allowlist=frozenset({12345678}),
            deployment_live_activation_enabled=True,
            controlled_account_validated=True,
        ),
    )
    broker = BrokerRouter(paper=paper, mt5_live=live)
    ledger = ExecutionLedger()
    service = ExecutionService(
        broker=broker,
        quote_source=quote_source,
        ledger=ledger,
        clock=clock.now,
    )
    return quote_source, broker, ledger, service


def test_restart_reconciles_unknown_without_resubmit(db_session):
    clock = FixedClock(datetime(2024, 6, 1, 15, 0, tzinfo=timezone.utc))
    account = create_paper_account(db_session, name="live-acct", initial_balance=Decimal("100000"))
    deployment = create_execution_deployment(
        db_session,
        paper_account_id=account.id,
        name="live-win",
        identity=strategy_identity(),
        broker_mode=BrokerMode.MT5_LIVE,
        lifecycle=DeploymentLifecycle.RUNNING,
        live_activation_enabled=True,
    )
    acquire_worker_lease(
        db_session,
        deployment_id=deployment.id,
        worker_id="worker-a",
        lease_token="token-1",
        ttl_seconds=60,
        now=clock.now(),
    )
    order = create_execution_order_intent(
        db_session,
        deployment_id=deployment.id,
        decision_id=None,
        broker_mode=BrokerMode.MT5_LIVE,
        side=ExecutionSide.BUY,
        quantity=Decimal("1"),
        intent_committed_at=clock.now(),
    )
    transition_execution_order(
        db_session,
        order.id,
        ExecutionOrderStatus.UNKNOWN,
        reconciliation_state=ReconciliationState.PENDING,
        rejection_reason="broker outcome unknown",
    )
    db_session.commit()

    with fake_edge_server() as (base_url, state):
        state.lookup_handlers[str(order.id)] = lambda _body: {
            "outcome": "filled",
            "closes_intent": True,
            "deals": [make_filled_lookup_deal(ticket=42, symbol="WIN$", volume=1.0, price=130010.0)],
        }
        _, broker, ledger, _ = _components(base_url, clock)
        reconciler = OrderReconciler(
            broker=broker,
            ledger=ledger,
            point_value=Decimal("0.2"),
            clock=clock.now,
        )
        outcomes = reconciler.reconcile_deployment(db_session, deployment)
        db_session.commit()
        assert outcomes[0].resolution.value == "filled"
        assert not any(r[1] == "/v1/submit" for r in state.requests)
        assert any(r[1] == "/v1/lookup" for r in state.requests)


def test_edge_down_blocks_new_orders_with_stale_quote(db_session, seeded_deployment, paper_cost_config):
    _, deployment, token = seeded_deployment
    clock = FixedClock(datetime(2024, 6, 1, 15, 0, tzinfo=timezone.utc))
    with fake_edge_server() as (base_url, state):
        state.unavailable = True
        _, _, _, service = _components(base_url, clock)
        result = service.process_completed_bar(
            db_session,
            deployment=deployment,
            eval_result=ForwardDecisionResult(
                deployment_id=str(deployment.id),
                bar_close_time=datetime(2024, 6, 1, 14, 0, tzinfo=timezone.utc),
                bar_close_price=130005.0,
                signal_action=SignalAction.BUY,
                reason="buy",
                requested_quantity=1.0,
                strategy_name="MACrossover",
                strategy_version=1,
                config_hash="test-hash",
                symbol="WIN$",
                timeframe="H1",
                timing=EvaluationPhaseTiming(total_ms=1.0),
            ),
            lease_token=token,
            worker_id="worker-a",
            cost_config=paper_cost_config,
            point_value=Decimal("0.2"),
        )
        assert result.outcome == DecisionOutcome.RISK_REJECTED
        assert list_orders_for_deployment(db_session, deployment.id) == []


def test_lookup_window_starts_before_intent(execution_clock):
    clock = execution_clock
    order_id = uuid4()
    intent_at = datetime(2024, 6, 1, 14, 0, tzinfo=timezone.utc)
    captured: list[dict] = []

    def _lookup_handler(body):
        captured.append(body)
        return {"outcome": "not_found", "closes_intent": True}

    with fake_edge_server() as (base_url, state):
        state.lookup_handlers[str(order_id)] = _lookup_handler
        broker = EdgeBroker(
            client=EdgeClient(base_url, timeouts=EdgeTimeouts()),
            clock=clock,
            gates=LiveExecutionGates(),
            lookup_window_lead_s=60.0,
        )
        request = MarketOrderRequest(
            deployment_id=uuid4(),
            order_id=order_id,
            symbol="WIN$",
            side=ExecutionSide.BUY,
            quantity=Decimal("1"),
            external_fill_id=f"mt5_live:{order_id}",
            broker_mode=BrokerMode.MT5_LIVE,
            intent_created_at=intent_at,
        )
        broker.lookup_order(request)
        assert captured
        assert captured[0]["window_start"] < intent_at.isoformat().replace("+00:00", "Z")
