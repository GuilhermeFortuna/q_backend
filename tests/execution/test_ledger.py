from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from q_backend.execution.brokers.base import MarketOrderRequest, PaperCostConfig
from q_backend.execution.brokers.paper import PaperBroker
from q_backend.execution.domain import (
    BrokerMode,
    ExecutionSide,
    PositionSide,
    StrategyIdentity,
)
from q_backend.execution.ledger import ExecutionLedger
from q_backend.storage.db.execution_models import ExecutionLedgerEntry
from q_backend.storage.db.execution_repositories import (
    create_execution_deployment,
    create_execution_order_intent,
    create_paper_account,
    get_open_net_position,
    get_paper_account,
)
from q_backend.execution.brokers.fakes import (
    FakeQuoteSource,
    FixedClock,
    fill_row_count,
    ledger_row_count,
)


@pytest.fixture
def now() -> datetime:
    return datetime(2026, 6, 30, 15, 0, tzinfo=timezone.utc)


@pytest.fixture
def cost_config() -> PaperCostConfig:
    return PaperCostConfig(
        point_value=Decimal("0.2"),
        slippage_points=Decimal("0"),
        cost_per_contract=Decimal("1"),
        cost_bps=Decimal("0"),
        max_quote_age_seconds=30.0,
    )


def _setup(db_session, now, *, spread: tuple[Decimal, Decimal]):
    account = create_paper_account(
        db_session,
        name=f"ledger-{uuid4().hex[:8]}",
        initial_balance=Decimal("100000"),
    )
    deployment = create_execution_deployment(
        db_session,
        paper_account_id=account.id,
        name="win-h1",
        identity=StrategyIdentity(
            strategy_name="demo",
            strategy_version=1,
            compiled_config={},
            config_hash="hash",
            symbol="WIN$",
            timeframe="H1",
        ),
    )
    clock = FixedClock(now)
    quotes = FakeQuoteSource({"WIN$": spread}, timestamp=now)
    broker = PaperBroker(quote_source=quotes, clock=clock)
    ledger = ExecutionLedger()
    return account, deployment, broker, ledger, quotes


def _submit_and_apply(
    db_session,
    *,
    account,
    deployment,
    broker,
    ledger,
    cost_config,
    side: ExecutionSide,
    quantity: Decimal,
    suffix: str,
):
    order = create_execution_order_intent(
        db_session,
        deployment_id=deployment.id,
        decision_id=None,
        broker_mode=BrokerMode.PAPER,
        side=side,
        quantity=quantity,
    )
    external_fill_id = f"paper:{order.id}:{suffix}"
    request = MarketOrderRequest(
        deployment_id=deployment.id,
        order_id=order.id,
        symbol="WIN$",
        side=side,
        quantity=quantity,
        external_fill_id=external_fill_id,
    )
    submission = broker.submit_market_order(request, cost_config=cost_config)
    assert submission.accepted and submission.fill is not None
    return ledger.apply_fill(
        db_session,
        paper_account_id=account.id,
        deployment_id=deployment.id,
        order_id=order.id,
        fill=submission.fill,
        point_value=cost_config.point_value,
        symbol="WIN$",
    )


def test_long_open_and_close_accounting(db_session, now, cost_config):
    account, deployment, broker, ledger, quotes = _setup(
        db_session, now, spread=(Decimal("100"), Decimal("101"))
    )
    open_result = _submit_and_apply(
        db_session,
        account=account,
        deployment=deployment,
        broker=broker,
        ledger=ledger,
        cost_config=cost_config,
        side=ExecutionSide.BUY,
        quantity=Decimal("2"),
        suffix="open-long",
    )
    assert open_result.position_side == PositionSide.LONG
    assert open_result.position_quantity == Decimal("2")
    assert open_result.realized_pnl == Decimal("0")
    assert open_result.cash_balance == Decimal("99998")

    quotes._quotes["WIN$"] = (Decimal("104"), Decimal("105"))
    close_result = _submit_and_apply(
        db_session,
        account=account,
        deployment=deployment,
        broker=broker,
        ledger=ledger,
        cost_config=cost_config,
        side=ExecutionSide.SELL,
        quantity=Decimal("2"),
        suffix="close-long",
    )
    assert close_result.position_side == PositionSide.FLAT
    assert close_result.realized_pnl == Decimal("1.2")
    assert close_result.cash_balance == Decimal("99997.2")


def test_short_open_and_cover(db_session, now, cost_config):
    account, deployment, broker, ledger, quotes = _setup(
        db_session, now, spread=(Decimal("200"), Decimal("201"))
    )
    _submit_and_apply(
        db_session,
        account=account,
        deployment=deployment,
        broker=broker,
        ledger=ledger,
        cost_config=cost_config,
        side=ExecutionSide.SELL,
        quantity=Decimal("1"),
        suffix="open-short",
    )
    position = get_open_net_position(db_session, deployment.id)
    assert position is not None
    assert position.side == PositionSide.SHORT.value

    quotes._quotes["WIN$"] = (Decimal("198"), Decimal("199"))
    cover = _submit_and_apply(
        db_session,
        account=account,
        deployment=deployment,
        broker=broker,
        ledger=ledger,
        cost_config=cost_config,
        side=ExecutionSide.BUY,
        quantity=Decimal("1"),
        suffix="cover-short",
    )
    assert cover.position_side == PositionSide.FLAT
    assert cover.realized_pnl == Decimal("0.2")


def test_explicit_reversal_long_to_short(db_session, now, cost_config):
    account, deployment, broker, ledger, _ = _setup(
        db_session, now, spread=(Decimal("100"), Decimal("101"))
    )
    _submit_and_apply(
        db_session,
        account=account,
        deployment=deployment,
        broker=broker,
        ledger=ledger,
        cost_config=cost_config,
        side=ExecutionSide.BUY,
        quantity=Decimal("1"),
        suffix="long-1",
    )
    reverse = _submit_and_apply(
        db_session,
        account=account,
        deployment=deployment,
        broker=broker,
        ledger=ledger,
        cost_config=cost_config,
        side=ExecutionSide.SELL,
        quantity=Decimal("3"),
        suffix="reverse",
    )
    assert reverse.position_side == PositionSide.SHORT
    assert reverse.position_quantity == Decimal("2")


def test_spread_round_trip_loses_spread(db_session, now):
    cost = PaperCostConfig(
        point_value=Decimal("1"),
        slippage_points=Decimal("0"),
        cost_per_contract=Decimal("0"),
        cost_bps=Decimal("0"),
    )
    account, deployment, broker, ledger, quotes = _setup(
        db_session, now, spread=(Decimal("100"), Decimal("102"))
    )
    start_cash = account.cash_balance
    _submit_and_apply(
        db_session,
        account=account,
        deployment=deployment,
        broker=broker,
        ledger=ledger,
        cost_config=cost,
        side=ExecutionSide.BUY,
        quantity=Decimal("1"),
        suffix="rt-buy",
    )
    _submit_and_apply(
        db_session,
        account=account,
        deployment=deployment,
        broker=broker,
        ledger=ledger,
        cost_config=cost,
        side=ExecutionSide.SELL,
        quantity=Decimal("1"),
        suffix="rt-sell",
    )
    account = get_paper_account(db_session, account.id)
    assert account is not None
    assert account.cash_balance == start_cash - Decimal("2")


def test_slippage_and_commission_once_per_side(db_session, now):
    cost = PaperCostConfig(
        point_value=Decimal("1"),
        slippage_points=Decimal("1"),
        cost_per_contract=Decimal("2"),
        cost_bps=Decimal("0"),
    )
    account, deployment, broker, ledger, _ = _setup(
        db_session, now, spread=(Decimal("100"), Decimal("101"))
    )
    buy = _submit_and_apply(
        db_session,
        account=account,
        deployment=deployment,
        broker=broker,
        ledger=ledger,
        cost_config=cost,
        side=ExecutionSide.BUY,
        quantity=Decimal("1"),
        suffix="fee-buy",
    )
    assert buy.fee == Decimal("2")
    sell = _submit_and_apply(
        db_session,
        account=account,
        deployment=deployment,
        broker=broker,
        ledger=ledger,
        cost_config=cost,
        side=ExecutionSide.SELL,
        quantity=Decimal("1"),
        suffix="fee-sell",
    )
    assert sell.fee == Decimal("2")
    fees = db_session.execute(
        select(func.coalesce(func.sum(ExecutionLedgerEntry.amount), 0)).where(
            ExecutionLedgerEntry.entry_type == "fee"
        )
    ).scalar_one()
    assert Decimal(str(fees)) == Decimal("-4")


def test_duplicate_fill_identity_is_idempotent(db_session, now, cost_config):
    account, deployment, broker, ledger, _ = _setup(
        db_session, now, spread=(Decimal("100"), Decimal("101"))
    )
    order = create_execution_order_intent(
        db_session,
        deployment_id=deployment.id,
        decision_id=None,
        broker_mode=BrokerMode.PAPER,
        side=ExecutionSide.BUY,
        quantity=Decimal("1"),
    )
    request = MarketOrderRequest(
        deployment_id=deployment.id,
        order_id=order.id,
        symbol="WIN$",
        side=ExecutionSide.BUY,
        quantity=Decimal("1"),
        external_fill_id=f"paper:{order.id}:dup",
    )
    submission = broker.submit_market_order(request, cost_config=cost_config)
    first = ledger.apply_fill(
        db_session,
        paper_account_id=account.id,
        deployment_id=deployment.id,
        order_id=order.id,
        fill=submission.fill,
        point_value=cost_config.point_value,
        symbol="WIN$",
    )
    second = ledger.apply_fill(
        db_session,
        paper_account_id=account.id,
        deployment_id=deployment.id,
        order_id=order.id,
        fill=submission.fill,
        point_value=cost_config.point_value,
        symbol="WIN$",
    )
    assert first.idempotent is False
    assert second.idempotent is True
    assert fill_row_count(db_session) == 1


def test_ledger_conservation_equity_components(db_session, now, cost_config):
    account, deployment, broker, ledger, quotes = _setup(
        db_session, now, spread=(Decimal("100"), Decimal("101"))
    )
    _submit_and_apply(
        db_session,
        account=account,
        deployment=deployment,
        broker=broker,
        ledger=ledger,
        cost_config=cost_config,
        side=ExecutionSide.BUY,
        quantity=Decimal("1"),
        suffix="eq-open",
    )
    quotes._quotes["WIN$"] = (Decimal("110"), Decimal("111"))
    snapshot = ledger.account_snapshot(
        db_session,
        paper_account_id=account.id,
        deployment_id=deployment.id,
        symbol="WIN$",
        quote=quotes.get_quote("WIN$"),
        point_value=cost_config.point_value,
        as_of=now,
    )
    assert snapshot.unrealized_pnl == Decimal("1.8")
    assert snapshot.equity == snapshot.cash_balance + snapshot.unrealized_pnl


def test_ledger_transaction_rollback_drops_partial_writes(db_engine, now, cost_config):
    session_factory = sessionmaker(
        bind=db_engine,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
    )
    session = session_factory()
    account, deployment, broker, ledger, _ = _setup(
        session, now, spread=(Decimal("100"), Decimal("101"))
    )
    order = create_execution_order_intent(
        session,
        deployment_id=deployment.id,
        decision_id=None,
        broker_mode=BrokerMode.PAPER,
        side=ExecutionSide.BUY,
        quantity=Decimal("1"),
    )
    submission = broker.submit_market_order(
        MarketOrderRequest(
            deployment_id=deployment.id,
            order_id=order.id,
            symbol="WIN$",
            side=ExecutionSide.BUY,
            quantity=Decimal("1"),
            external_fill_id=f"paper:{order.id}:rollback",
        ),
        cost_config=cost_config,
    )
    session.commit()
    deployment_id = deployment.id
    account_id = account.id
    try:
        ledger.apply_fill(
            session,
            paper_account_id=account_id,
            deployment_id=deployment_id,
            order_id=order.id,
            fill=submission.fill,
            point_value=cost_config.point_value,
            symbol="WIN$",
        )
        raise RuntimeError("simulated failure")
    except RuntimeError:
        session.rollback()
    finally:
        session.close()

    verify = session_factory()
    try:
        assert get_open_net_position(verify, deployment_id) is None
        assert fill_row_count(verify) == 0
        assert ledger_row_count(verify) == 0
        acct = get_paper_account(verify, account_id)
        assert acct is not None
        assert acct.cash_balance == Decimal("100000")
    finally:
        verify.close()
