"""Execution API route and validation tests."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from q_backend.api.schemas.execution import (
    DeploymentActionRequest,
    DeploymentCreateRequest,
    DeploymentIdentityInput,
    KillSwitchUpdateRequest,
    PaperAccountCreateRequest,
)
from q_backend.api.services import execution as execution_service
from q_backend.execution.domain import DeploymentLifecycle, StrategyIdentity
from q_backend.execution.validation import compute_config_hash
from q_backend.storage.db.base import Base
from q_backend.storage.db import models  # noqa: F401
from q_backend.storage.db import execution_models  # noqa: F401
from q_backend.storage.db.execution_repositories import (
    acquire_worker_lease,
    create_execution_deployment,
    create_paper_account,
    list_audit_events_page,
)
from q_backend.storage.db.repositories import (
    create_backtest_config,
    create_backtest_run,
    update_backtest_run,
)


@pytest.fixture
def api_db_engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    yield engine
    Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture
def api_db_session(api_db_engine) -> Session:
    session = sessionmaker(
        bind=api_db_engine,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
    )()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _identity_payload(*, symbol: str = "WIN$", timeframe: str = "H1") -> DeploymentIdentityInput:
    compiled = {
        "strategy": "MACrossover",
        "strategy_params": {"fast_period": 5, "slow_period": 20},
        "symbol": symbol,
        "timeframe": timeframe,
    }
    return DeploymentIdentityInput(
        strategy_name="MACrossover",
        strategy_version=1,
        compiled_config=compiled,
        config_hash=compute_config_hash(compiled),
        symbol=symbol,
        timeframe=timeframe,
        sizing_config={"type": "fixed_quantity", "quantity": 1.0},
        risk_config={"max_daily_loss": "5000", "max_notional": "1000000"},
    )


def test_create_and_list_paper_account(api_db_session: Session):
    created = execution_service.create_account(
        api_db_session,
        PaperAccountCreateRequest(
            name="desk-1",
            initial_balance=Decimal("100000"),
        ),
    )
    listing = execution_service.list_accounts(api_db_session, limit=50, offset=0)
    assert listing.total == 1
    assert listing.items[0].id == created.id
    assert listing.items[0].initial_balance == Decimal("100000")


def test_create_deployment_with_identity(api_db_session: Session):
    account = execution_service.create_account(
        api_db_session,
        PaperAccountCreateRequest(name="desk-2", initial_balance=Decimal("50000")),
    )
    detail = execution_service.create_deployment(
        api_db_session,
        DeploymentCreateRequest(
            paper_account_id=account.id,
            name="win-h1",
            identity=_identity_payload(),
        ),
    )
    assert detail.lifecycle == DeploymentLifecycle.DRAFT.value
    assert detail.broker_mode == "paper"
    assert detail.config_hash == _identity_payload().config_hash


def test_create_deployment_from_saved_backtest(api_db_session: Session):
    account = execution_service.create_account(
        api_db_session,
        PaperAccountCreateRequest(name="desk-3", initial_balance=Decimal("50000")),
    )
    config = create_backtest_config(
        api_db_session,
        name="win-h1-config",
        config={"symbol": "WIN$", "timeframe": "H1"},
    )
    run = create_backtest_run(
        api_db_session,
        backtest_config_id=config.id,
        config={
            "strategy": "MACrossover",
            "strategy_params": {"fast_period": 5, "slow_period": 20},
            "symbol": "WIN$",
            "timeframe": "H1",
            "engine": "candle",
        },
        status="completed",
    )
    update_backtest_run(api_db_session, run.id, is_saved=True)
    detail = execution_service.create_deployment(
        api_db_session,
        DeploymentCreateRequest(
            paper_account_id=account.id,
            name="from-backtest",
            source_backtest_run_id=run.id,
        ),
    )
    assert detail.strategy_name == "MACrossover"
    assert detail.symbol == "WIN$"


def test_reject_m1_timeframe(api_db_session: Session):
    account = execution_service.create_account(
        api_db_session,
        PaperAccountCreateRequest(name="desk-4", initial_balance=Decimal("10000")),
    )
    with pytest.raises(HTTPException) as exc:
        execution_service.create_deployment(
            api_db_session,
            DeploymentCreateRequest(
                paper_account_id=account.id,
                name="too-fast",
                identity=_identity_payload(symbol="WIN$", timeframe="M1"),
            ),
        )
    assert "M15" in str(exc.value.detail)


def test_reject_live_activation(api_db_session: Session):
    account = execution_service.create_account(
        api_db_session,
        PaperAccountCreateRequest(name="desk-5", initial_balance=Decimal("10000")),
    )
    with pytest.raises(HTTPException) as exc:
        execution_service.create_deployment(
            api_db_session,
            DeploymentCreateRequest(
                paper_account_id=account.id,
                name="live-locked",
                live_activation_enabled=True,
                identity=_identity_payload(),
            ),
        )
    assert "locked" in str(exc.value.detail).lower()


def test_deployment_lifecycle_actions(api_db_session: Session):
    account = execution_service.create_account(
        api_db_session,
        PaperAccountCreateRequest(name="desk-6", initial_balance=Decimal("10000")),
    )
    deployment = execution_service.create_deployment(
        api_db_session,
        DeploymentCreateRequest(
            paper_account_id=account.id,
            name="lifecycle",
            identity=_identity_payload(),
        ),
    )
    started = execution_service.apply_deployment_action(
        api_db_session,
        deployment.id,
        DeploymentActionRequest(action="start", confirm=True),
    )
    assert started.lifecycle == DeploymentLifecycle.RUNNING.value
    paused = execution_service.apply_deployment_action(
        api_db_session,
        deployment.id,
        DeploymentActionRequest(action="pause", confirm=True),
    )
    assert paused.lifecycle == DeploymentLifecycle.PAUSED.value


def test_flatten_requires_confirmation(api_db_session: Session):
    account = execution_service.create_account(
        api_db_session,
        PaperAccountCreateRequest(name="desk-7", initial_balance=Decimal("10000")),
    )
    deployment = execution_service.create_deployment(
        api_db_session,
        DeploymentCreateRequest(
            paper_account_id=account.id,
            name="flatten",
            identity=_identity_payload(),
        ),
    )
    with pytest.raises(HTTPException) as exc:
        execution_service.apply_deployment_action(
            api_db_session,
            deployment.id,
            DeploymentActionRequest(action="flatten", confirm=False),
        )
    assert "confirm" in str(exc.value.detail).lower()


def test_flatten_queues_pending_action_and_audit(api_db_session: Session):
    account = execution_service.create_account(
        api_db_session,
        PaperAccountCreateRequest(name="desk-8", initial_balance=Decimal("10000")),
    )
    deployment = execution_service.create_deployment(
        api_db_session,
        DeploymentCreateRequest(
            paper_account_id=account.id,
            name="flatten-ok",
            identity=_identity_payload(),
        ),
    )
    result = execution_service.apply_deployment_action(
        api_db_session,
        deployment.id,
        DeploymentActionRequest(action="flatten", confirm=True, actor="operator"),
    )
    assert result.pending_action == "flatten"
    events, total = list_audit_events_page(api_db_session, deployment_id=deployment.id)
    assert total == 1
    assert events[0].event_type == "deployment_flatten"


def test_kill_switch_confirmation_and_audit(api_db_session: Session):
    with pytest.raises(HTTPException) as exc:
        execution_service.update_kill_switch(
            api_db_session,
            KillSwitchUpdateRequest(enabled=True, confirm=False, reason="test"),
        )
    assert "confirm" in str(exc.value.detail).lower()

    updated = execution_service.update_kill_switch(
        api_db_session,
        KillSwitchUpdateRequest(
            enabled=True,
            confirm=True,
            reason="maintenance",
            updated_by="operator",
        ),
    )
    assert updated.accepted is True
    assert updated.kill_switch.enabled is True
    events, total = list_audit_events_page(api_db_session, event_type="kill_switch_enabled")
    assert total == 1


def test_health_worker_offline_fixture(api_db_session: Session):
    health = execution_service.get_execution_health(
        api_db_session,
        mt5_connected=False,
    )
    assert health.api_status in {"ok", "degraded"}
    assert health.worker_status == "offline"
    assert health.market_data_status == "offline"
    assert health.live_capability_locked is True


def test_health_with_running_deployment_and_lease(api_db_session: Session):
    account = create_paper_account(api_db_session, name="health-account", initial_balance=Decimal("100000"))
    deployment = create_execution_deployment(
        api_db_session,
        paper_account_id=account.id,
        name="health-dep",
        identity=StrategyIdentity(
            strategy_name="MACrossover",
            strategy_version=1,
            compiled_config=_identity_payload().compiled_config,
            config_hash=_identity_payload().config_hash,
            symbol="WIN$",
            timeframe="H1",
            sizing_config={"type": "fixed_quantity", "quantity": 1.0},
        ),
        lifecycle=DeploymentLifecycle.RUNNING,
    )
    now = datetime.now(timezone.utc)
    acquire_worker_lease(
        api_db_session,
        deployment_id=deployment.id,
        worker_id="worker-a",
        lease_token="token",
        ttl_seconds=120,
        now=now,
    )
    health = execution_service.get_execution_health(api_db_session, mt5_connected=True)
    assert health.worker_status == "healthy"
    assert len(health.deployments) == 1


def test_paginated_decisions_empty(api_db_session: Session):
    account = execution_service.create_account(
        api_db_session,
        PaperAccountCreateRequest(name="desk-9", initial_balance=Decimal("10000")),
    )
    deployment = execution_service.create_deployment(
        api_db_session,
        DeploymentCreateRequest(
            paper_account_id=account.id,
            name="empty-history",
            identity=_identity_payload(),
        ),
    )
    page = execution_service.list_decisions(
        api_db_session, deployment.id, from_time=None, to_time=None, limit=10, offset=0
    )
    assert page.total == 0
    assert page.items == []


def test_database_unavailable_returns_503():
    session = object()
    with patch(
        "q_backend.api.services.execution.list_paper_accounts",
        side_effect=SQLAlchemyError("db down"),
    ):
        with pytest.raises(HTTPException) as exc:
            execution_service.list_accounts(session, limit=10, offset=0)
    assert exc.value.status_code == 503
    assert "not accepted" in exc.value.detail


def _seed_pending_unknown(session):
    from q_backend.execution.domain import (
        BrokerMode,
        DecisionOutcome,
        ExecutionSide,
        SignalAction,
    )
    from q_backend.storage.db.execution_repositories import (
        create_execution_decision,
        create_execution_order_intent,
        mark_incomplete_orders_unknown,
    )

    account = create_paper_account(session, name="recon-acct", initial_balance=Decimal("100000"))
    deployment = create_execution_deployment(
        session,
        paper_account_id=account.id,
        name="recon-dep",
        identity=StrategyIdentity(
            strategy_name="MACrossover",
            strategy_version=1,
            compiled_config=_identity_payload().compiled_config,
            config_hash=_identity_payload().config_hash,
            symbol="WIN$",
            timeframe="H1",
            sizing_config={"type": "fixed_quantity", "quantity": 1.0},
        ),
        lifecycle=DeploymentLifecycle.RUNNING,
    )
    decision = create_execution_decision(
        session,
        deployment_id=deployment.id,
        bar_close_time=datetime(2024, 6, 1, 14, 0, tzinfo=timezone.utc),
        identity=StrategyIdentity(
            strategy_name="MACrossover",
            strategy_version=1,
            compiled_config=_identity_payload().compiled_config,
            config_hash=_identity_payload().config_hash,
            symbol="WIN$",
            timeframe="H1",
            sizing_config={"type": "fixed_quantity", "quantity": 1.0},
        ),
        signal_action=SignalAction.BUY,
        outcome=DecisionOutcome.SIGNAL,
        requested_quantity=Decimal("1"),
    )
    order = create_execution_order_intent(
        session,
        deployment_id=deployment.id,
        decision_id=decision.id,
        broker_mode=BrokerMode.PAPER,
        side=ExecutionSide.BUY,
        quantity=Decimal("1"),
    )
    mark_incomplete_orders_unknown(session, deployment.id)
    session.flush()
    session.refresh(order)
    return account, deployment, order


def test_pending_reconciliation_list_and_manual_resolve(api_db_session: Session):
    from q_backend.api.schemas.execution import OrderResolutionRequest

    _, deployment, order = _seed_pending_unknown(api_db_session)

    listing = execution_service.list_pending_reconciliation(api_db_session, deployment.id, limit=50, offset=0)
    assert listing.total == 1
    assert listing.items[0].id == order.id
    assert listing.items[0].reconciliation_state == "pending"

    detail = execution_service.get_deployment_detail(api_db_session, deployment.id)
    assert detail.unknown_order_count == 1

    resolved = execution_service.resolve_order(
        api_db_session,
        order.id,
        OrderResolutionRequest(
            outcome="filled",
            actor="operator-9",
            reason="verified filled in MT5 terminal",
            price=Decimal("130010"),
        ),
    )
    assert resolved.accepted is True
    assert resolved.resolution == "filled"
    assert resolved.order.status == "filled"
    assert resolved.order.reconciled_by == "operator-9"

    # No longer pending / unknown.
    after = execution_service.list_pending_reconciliation(api_db_session, deployment.id, limit=50, offset=0)
    assert after.total == 0
    detail_after = execution_service.get_deployment_detail(api_db_session, deployment.id)
    assert detail_after.unknown_order_count == 0

    events, _ = list_audit_events_page(
        api_db_session, deployment_id=deployment.id, event_type="order_reconciliation_resolved"
    )
    assert len(events) == 1


def test_manual_resolve_filled_requires_price(api_db_session: Session):
    from pydantic import ValidationError

    from q_backend.api.schemas.execution import OrderResolutionRequest

    with pytest.raises(ValidationError):
        OrderResolutionRequest(outcome="filled", actor="op", reason="why")


def test_manual_resolve_conflict_when_not_pending(api_db_session: Session):
    from q_backend.api.schemas.execution import OrderResolutionRequest

    _, _, order = _seed_pending_unknown(api_db_session)
    execution_service.resolve_order(
        api_db_session,
        order.id,
        OrderResolutionRequest(outcome="not_filled", actor="op", reason="no fill"),
    )
    with pytest.raises(HTTPException) as exc:
        execution_service.resolve_order(
            api_db_session,
            order.id,
            OrderResolutionRequest(outcome="not_filled", actor="op", reason="again"),
        )
    assert exc.value.status_code == 409


def test_openapi_includes_execution_paths():
    from q_backend.api.main import app

    paths = {route.path for route in app.routes if hasattr(route, "path")}
    assert "/api/v1/execution/health" in paths
    assert "/api/v1/execution/kill-switch" in paths
    assert "/api/v1/execution/deployments/{deployment_id}/reconciliation" in paths
    assert "/api/v1/execution/orders/{order_id}/resolve" in paths
