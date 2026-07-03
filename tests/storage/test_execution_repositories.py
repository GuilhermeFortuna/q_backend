from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from q_backend.execution.domain import (
    BrokerMode,
    DecisionOutcome,
    DeploymentLifecycle,
    ExecutionOrderStatus,
    ExecutionSide,
    IllegalLifecycleTransition,
    LedgerEntryType,
    PositionSide,
    SignalAction,
    StrategyIdentity,
)
from q_backend.storage.db.execution_models import ExecutionNetPosition
from q_backend.storage.db.execution_repositories import (
    LeaseConflictError,
    acquire_worker_lease,
    append_ledger_entry,
    create_execution_decision,
    create_execution_deployment,
    create_execution_fill,
    create_execution_order_intent,
    create_paper_account,
    get_open_net_position,
    transition_deployment_lifecycle,
    transition_execution_order,
    upsert_open_net_position,
)


def _identity() -> StrategyIdentity:
    return StrategyIdentity(
        strategy_name="demo_strategy",
        strategy_version=3,
        compiled_config={"entry": "rsi_cross"},
        config_hash="abc123",
        symbol="WIN$",
        timeframe="H1",
        sizing_config={"mode": "fixed", "quantity": "1"},
        risk_config={"max_daily_loss": "500"},
    )


def _seed_account_and_deployment(session: Session):
    account = create_paper_account(
        session,
        name="paper-main",
        initial_balance=Decimal("100000.00"),
    )
    deployment = create_execution_deployment(
        session,
        paper_account_id=account.id,
        name="win-h1-demo",
        identity=_identity(),
    )
    return account, deployment


def test_decimal_round_trip_b3_style_prices_and_quantities(db_session: Session):
    account = create_paper_account(
        db_session,
        name="decimal-account",
        initial_balance=Decimal("250000.50"),
    )
    assert account.cash_balance == Decimal("250000.50")

    deployment = create_execution_deployment(
        db_session,
        paper_account_id=account.id,
        name="wdo-m15",
        identity=StrategyIdentity(
            strategy_name="wdo",
            strategy_version=1,
            compiled_config={},
            config_hash="h1",
            symbol="WDO$",
            timeframe="M15",
        ),
    )
    order = create_execution_order_intent(
        db_session,
        deployment_id=deployment.id,
        decision_id=None,
        broker_mode=BrokerMode.PAPER,
        side=ExecutionSide.BUY,
        quantity=Decimal("2.5"),
    )
    fill = create_execution_fill(
        db_session,
        deployment_id=deployment.id,
        order_id=order.id,
        broker_mode=BrokerMode.PAPER,
        external_fill_id="paper:fill-1",
        side=ExecutionSide.BUY,
        quantity=Decimal("2.5"),
        price=Decimal("5423.50"),
        fee=Decimal("1.25"),
        slippage=Decimal("0.50"),
        quote_bid=Decimal("5423.00"),
        quote_ask=Decimal("5423.50"),
        filled_at=datetime(2026, 6, 30, 15, 0, tzinfo=timezone.utc),
    )
    db_session.commit()
    db_session.expire_all()

    persisted = db_session.get(type(fill), fill.id)
    assert persisted is not None
    assert persisted.quantity == Decimal("2.5")
    assert persisted.price == Decimal("5423.50")
    assert persisted.fee == Decimal("1.25")
    assert persisted.quote_bid == Decimal("5423.00")


def test_unique_decision_per_deployment_bar_close(db_session: Session):
    _, deployment = _seed_account_and_deployment(db_session)
    identity = _identity()
    bar_close = datetime(2026, 6, 30, 14, 0, tzinfo=timezone.utc)
    create_execution_decision(
        db_session,
        deployment_id=deployment.id,
        bar_close_time=bar_close,
        identity=identity,
        signal_action=SignalAction.BUY,
        outcome=DecisionOutcome.SIGNAL,
        requested_quantity=Decimal("1"),
    )
    with pytest.raises(IntegrityError):
        create_execution_decision(
            db_session,
            deployment_id=deployment.id,
            bar_close_time=bar_close,
            identity=identity,
            signal_action=SignalAction.BUY,
            outcome=DecisionOutcome.SIGNAL,
            requested_quantity=Decimal("1"),
        )
        db_session.flush()
    db_session.rollback()


def test_unique_fill_broker_external_id(db_session: Session):
    account, deployment = _seed_account_and_deployment(db_session)
    order = create_execution_order_intent(
        db_session,
        deployment_id=deployment.id,
        decision_id=None,
        broker_mode=BrokerMode.PAPER,
        side=ExecutionSide.BUY,
        quantity=Decimal("1"),
    )
    filled_at = datetime(2026, 6, 30, 15, 0, tzinfo=timezone.utc)
    create_execution_fill(
        db_session,
        deployment_id=deployment.id,
        order_id=order.id,
        broker_mode=BrokerMode.PAPER,
        external_fill_id="paper:dup",
        side=ExecutionSide.BUY,
        quantity=Decimal("1"),
        price=Decimal("100"),
        filled_at=filled_at,
    )
    order2 = create_execution_order_intent(
        db_session,
        deployment_id=deployment.id,
        decision_id=None,
        broker_mode=BrokerMode.PAPER,
        side=ExecutionSide.SELL,
        quantity=Decimal("1"),
    )
    with pytest.raises(IntegrityError):
        create_execution_fill(
            db_session,
            deployment_id=deployment.id,
            order_id=order2.id,
            broker_mode=BrokerMode.PAPER,
            external_fill_id="paper:dup",
            side=ExecutionSide.SELL,
            quantity=Decimal("1"),
            price=Decimal("101"),
            filled_at=filled_at,
        )
        db_session.flush()
    db_session.rollback()


def test_one_open_net_position_per_deployment(db_session: Session):
    _, deployment = _seed_account_and_deployment(db_session)
    upsert_open_net_position(
        db_session,
        deployment_id=deployment.id,
        side=PositionSide.LONG,
        quantity=Decimal("1"),
        average_entry_price=Decimal("100"),
    )
    with pytest.raises(IntegrityError):
        db_session.add(
            ExecutionNetPosition(
                deployment_id=deployment.id,
                side=PositionSide.SHORT.value,
                quantity=Decimal("2"),
                average_entry_price=Decimal("99"),
                is_open=True,
            )
        )
        db_session.flush()
    db_session.rollback()


def test_one_open_net_position_upsert_updates_existing(db_session: Session):
    _, deployment = _seed_account_and_deployment(db_session)
    first = upsert_open_net_position(
        db_session,
        deployment_id=deployment.id,
        side=PositionSide.LONG,
        quantity=Decimal("1"),
        average_entry_price=Decimal("100"),
    )
    second = upsert_open_net_position(
        db_session,
        deployment_id=deployment.id,
        side=PositionSide.LONG,
        quantity=Decimal("2"),
        average_entry_price=Decimal("101"),
    )
    assert first.id == second.id
    assert second.quantity == Decimal("2")
    assert get_open_net_position(db_session, deployment.id) is not None


def test_one_lease_per_deployment(db_session: Session):
    _, deployment = _seed_account_and_deployment(db_session)
    now = datetime(2026, 6, 30, 12, 0, tzinfo=timezone.utc)
    acquire_worker_lease(
        db_session,
        deployment_id=deployment.id,
        worker_id="worker-a",
        lease_token="token-a",
        ttl_seconds=30,
        now=now,
    )
    with pytest.raises(LeaseConflictError):
        acquire_worker_lease(
            db_session,
            deployment_id=deployment.id,
            worker_id="worker-b",
            lease_token="token-b",
            ttl_seconds=30,
            now=now + timedelta(seconds=1),
        )


def test_repository_transaction_rollback_drops_partial_ledger_and_position(
    db_engine,
):
    session_factory = sessionmaker(
        bind=db_engine,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
    )
    session = session_factory()
    account, deployment = _seed_account_and_deployment(session)
    try:
        upsert_open_net_position(
            session,
            deployment_id=deployment.id,
            side=PositionSide.LONG,
            quantity=Decimal("1"),
            average_entry_price=Decimal("100"),
        )
        append_ledger_entry(
            session,
            paper_account_id=account.id,
            deployment_id=deployment.id,
            entry_type=LedgerEntryType.FILL_CASH,
            amount=Decimal("-100"),
            balance_after=Decimal("99900"),
        )
        raise RuntimeError("simulated failure before commit")
    except RuntimeError:
        session.rollback()
    finally:
        session.close()

    verify = session_factory()
    try:
        assert get_open_net_position(verify, deployment.id) is None
        from sqlalchemy import func, select

        from q_backend.storage.db.execution_models import ExecutionLedgerEntry

        count = verify.execute(
            select(func.count()).select_from(ExecutionLedgerEntry)
        ).scalar_one()
        assert count == 0
    finally:
        verify.close()


def test_deployment_lifecycle_repository_enforces_transitions(db_session: Session):
    _, deployment = _seed_account_and_deployment(db_session)
    transition_deployment_lifecycle(
        db_session, deployment.id, DeploymentLifecycle.RUNNING
    )
    with pytest.raises(IllegalLifecycleTransition):
        transition_deployment_lifecycle(
            db_session, deployment.id, DeploymentLifecycle.DRAFT
        )


def test_order_lifecycle_repository_enforces_transitions(db_session: Session):
    _, deployment = _seed_account_and_deployment(db_session)
    order = create_execution_order_intent(
        db_session,
        deployment_id=deployment.id,
        decision_id=None,
        broker_mode=BrokerMode.PAPER,
        side=ExecutionSide.BUY,
        quantity=Decimal("1"),
    )
    transition_execution_order(
        db_session, order.id, ExecutionOrderStatus.SUBMITTED
    )
    with pytest.raises(IllegalLifecycleTransition):
        transition_execution_order(
            db_session, order.id, ExecutionOrderStatus.INTENT
        )


def test_execution_migration_revision_chain() -> None:
    alembic_cfg = Config("alembic.ini")
    script = ScriptDirectory.from_config(alembic_cfg)

    # Full linear migration chain from base to head. Assert the whole ordered
    # chain (rather than a single hardcoded down_revision pair) so that adding
    # the next migration updates this one list instead of silently going stale.
    expected_chain = [
        "20260607_0001",
        "20260609_0002",
        "20260611_0003",
        "20260613_0004",
        "20260613_0005",
        "20260621_0006",
        "20260621_0007",
        "20260622_0008",
        "20260626_0009",
        "20260626_0010",
        "20260626_0011",
        "20260628_0012",
        "20260628_0013",
            "20260630_0014",
            "20260630_0015",
            "20260702_0016",
        ]

    assert script.get_current_head() == expected_chain[-1]

    # Walk the chain from head back to base and confirm it is linear and matches.
    actual_chain: list[str] = []
    for revision in script.walk_revisions():
        actual_chain.append(revision.revision)
        down = revision.down_revision
        assert down is None or isinstance(down, str), (
            f"revision {revision.revision} has a non-linear down_revision: {down!r}"
        )

    assert list(reversed(actual_chain)) == expected_chain

    head_revision = script.get_revision(expected_chain[-1])
    assert head_revision is not None
    assert head_revision.down_revision == expected_chain[-2]
    assert callable(head_revision.module.upgrade)
    assert callable(head_revision.module.downgrade)
