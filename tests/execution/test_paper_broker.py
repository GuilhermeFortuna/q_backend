from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

import pytest

from q_backend.execution.brokers.base import (
    BrokerRejectionCode,
    MarketOrderRequest,
    PaperCostConfig,
)
from q_backend.execution.brokers.paper import (
    PaperBroker,
    compute_commission,
    executable_fill_price,
    validate_quote,
)
from q_backend.execution.domain import BrokerMode, ExecutionSide, StrategyIdentity
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
        slippage_points=Decimal("1"),
        cost_per_contract=Decimal("2"),
        cost_bps=Decimal("0"),
        max_quote_age_seconds=30.0,
        volume_step=Decimal("1"),
        min_volume=Decimal("1"),
    )


@pytest.fixture
def seeded(db_session, now):
    account = create_paper_account(
        db_session,
        name=f"acct-{uuid4().hex[:8]}",
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
    quotes = FakeQuoteSource({"WIN$": (Decimal("100"), Decimal("101"))}, timestamp=now)
    broker = PaperBroker(quote_source=quotes, clock=clock)
    return {
        "account": account,
        "deployment": deployment,
        "broker": broker,
        "clock": clock,
        "quotes": quotes,
    }


def _order_request(deployment_id, side, quantity, suffix: str) -> MarketOrderRequest:
    order_id = uuid4()
    return MarketOrderRequest(
        deployment_id=deployment_id,
        order_id=order_id,
        symbol="WIN$",
        side=side,
        quantity=quantity,
        external_fill_id=f"paper:{order_id}:{suffix}",
    )


def test_buy_fills_at_ask_plus_slippage(seeded, cost_config):
    req = _order_request(seeded["deployment"].id, ExecutionSide.BUY, Decimal("1"), "buy")
    result = seeded["broker"].submit_market_order(req, cost_config=cost_config)
    assert result.accepted
    assert result.fill is not None
    assert result.fill.price == Decimal("102")
    assert result.fill.fee == Decimal("2")
    assert result.fill.quote_bid == Decimal("100")
    assert result.fill.quote_ask == Decimal("101")


def test_sell_fills_at_bid_minus_slippage(seeded, cost_config):
    req = _order_request(seeded["deployment"].id, ExecutionSide.SELL, Decimal("1"), "sell")
    result = seeded["broker"].submit_market_order(req, cost_config=cost_config)
    assert result.accepted
    assert result.fill is not None
    assert result.fill.price == Decimal("99")


def test_stale_quote_rejected(seeded, cost_config, now):
    seeded["clock"].advance(60)
    req = _order_request(seeded["deployment"].id, ExecutionSide.BUY, Decimal("1"), "stale")
    result = seeded["broker"].submit_market_order(req, cost_config=cost_config)
    assert not result.accepted
    assert result.rejection is not None
    assert result.rejection.code == BrokerRejectionCode.STALE_QUOTE


def test_invalid_quantity_rejected(seeded, cost_config):
    req = _order_request(seeded["deployment"].id, ExecutionSide.BUY, Decimal("0"), "zero")
    result = seeded["broker"].submit_market_order(req, cost_config=cost_config)
    assert not result.accepted
    assert result.rejection.code == BrokerRejectionCode.INVALID_QUANTITY


def test_crossed_quote_rejected(now, cost_config):
    clock = FixedClock(now)
    quotes = FakeQuoteSource({"WIN$": (Decimal("101"), Decimal("100"))}, timestamp=now)
    broker = PaperBroker(quote_source=quotes, clock=clock)
    req = _order_request(uuid4(), ExecutionSide.BUY, Decimal("1"), "crossed")
    result = broker.submit_market_order(req, cost_config=cost_config)
    assert not result.accepted
    assert result.rejection.code == BrokerRejectionCode.INVALID_QUOTE


def test_broker_rejection_leaves_ledger_untouched(db_session, seeded, cost_config, now):
    before_ledger = ledger_row_count(db_session)
    before_fills = fill_row_count(db_session)
    seeded["clock"].advance(120)
    req = _order_request(seeded["deployment"].id, ExecutionSide.BUY, Decimal("1"), "no-ledger")
    result = seeded["broker"].submit_market_order(req, cost_config=cost_config)
    assert not result.accepted
    assert ledger_row_count(db_session) == before_ledger
    assert fill_row_count(db_session) == before_fills


def test_commission_applies_once_per_side(cost_config):
    price = Decimal("100")
    qty = Decimal("2")
    once = compute_commission(cost_config, price=price, quantity=qty)
    twice = once * 2
    assert once == Decimal("4")


def test_executable_price_helpers(now):
    from q_backend.execution.brokers.base import ExecutableQuote

    quote = ExecutableQuote(
        symbol="WIN$",
        bid=Decimal("100"),
        ask=Decimal("101"),
        timestamp=now,
    )
    assert executable_fill_price(ExecutionSide.BUY, quote, Decimal("1")) == Decimal("102")
    assert executable_fill_price(ExecutionSide.SELL, quote, Decimal("1")) == Decimal("99")


def test_validate_quote_missing(now):
    rejection = validate_quote(None, now=now, max_age_seconds=30)
    assert rejection is not None
    assert rejection.code == BrokerRejectionCode.QUOTE_UNAVAILABLE
