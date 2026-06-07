import pytest
from datetime import datetime, timezone
from q_backend.backtesting.models import (
    Order,
    Trade,
    OrderAction,
    OrderType,
    OrderStatus,
    TradeStatus,
)
from q_backend.backtesting.registry import TradeRegistry


def test_register_order():
    registry = TradeRegistry()
    order = Order(
        id="order-1",
        symbol="EURUSD",
        action=OrderAction.BUY,
        order_type=OrderType.MARKET,
        quantity=1.0,
    )
    registry.register_order(order)

    assert "order-1" in registry.orders
    assert registry.orders["order-1"].symbol == "EURUSD"


def test_register_and_close_trade():
    registry = TradeRegistry()

    trade = Trade(
        id="trade-1",
        order_id="order-1",
        symbol="EURUSD",
        action=OrderAction.BUY,
        quantity=1.0,
        entry_time=datetime(2023, 1, 1, 10, 0, tzinfo=timezone.utc),
        entry_price=1.1000,
        commission=5.0,
    )

    registry.register_trade(trade)

    open_trades = registry.get_open_trades()
    assert len(open_trades) == 1
    assert open_trades[0].id == "trade-1"

    # Close trade profitably
    exit_time = datetime(2023, 1, 1, 11, 0, tzinfo=timezone.utc)
    registry.close_trade("trade-1", exit_time=exit_time, exit_price=1.1050)

    assert trade.status == TradeStatus.CLOSED
    # (1.1050 - 1.1000) * 1.0 = 0.0050. Let's say quantity is e.g. 100000 for standard lot,
    # but here quantity=1.0.
    # PnL = (1.1050 - 1.1000) * 1.0 - 5.0 = 0.005 - 5.0 = -4.995
    assert trade.pnl == pytest.approx(-4.995)

    assert len(registry.get_open_trades()) == 0
    assert len(registry.get_closed_trades()) == 1


def test_performance_metrics():
    registry = TradeRegistry()

    trade1 = Trade(
        id="t1",
        order_id="o1",
        symbol="EURUSD",
        action=OrderAction.BUY,
        quantity=100000,
        entry_time=datetime.now(timezone.utc),
        entry_price=1.1000,
    )
    trade2 = Trade(
        id="t2",
        order_id="o2",
        symbol="EURUSD",
        action=OrderAction.SELL,
        quantity=100000,
        entry_time=datetime.now(timezone.utc),
        entry_price=1.1100,
    )

    registry.register_trade(trade1)
    registry.register_trade(trade2)

    registry.close_trade(
        "t1", datetime.now(timezone.utc), exit_price=1.1050
    )  # +500 PnL
    registry.close_trade(
        "t2", datetime.now(timezone.utc), exit_price=1.1080
    )  # +200 PnL

    metrics = registry.get_performance_metrics()

    assert metrics["total_trades"] == 2
    assert metrics["total_pnl"] == pytest.approx(700.0)
    assert metrics["win_rate"] == 1.0
    assert metrics["winning_trades"] == 2
    assert metrics["losing_trades"] == 0


def test_registry_advanced_metrics():
    """
    Tests that get_performance_metrics computes advanced statistics (drawdown, profit factor,
    consecutive wins/losses, expectancy) correctly based on a sequence of closed trades.
    """
    registry = TradeRegistry()

    # Deterministic sequence:
    # Initial Capital = $10,000
    # Trade 1: +$2,000 (Win)
    # Trade 2: -$1,500 (Loss)
    # Trade 3: -$500 (Loss)
    # Trade 4: +$3,000 (Win)
    t1 = Trade(
        id="t1",
        order_id="o1",
        symbol="CCM$",
        action=OrderAction.BUY,
        quantity=1,
        entry_time=datetime(2023, 1, 1, 10, 0, tzinfo=timezone.utc),
        entry_price=10.0,
    )
    t2 = Trade(
        id="t2",
        order_id="o2",
        symbol="CCM$",
        action=OrderAction.BUY,
        quantity=1,
        entry_time=datetime(2023, 1, 2, 10, 0, tzinfo=timezone.utc),
        entry_price=10.0,
    )
    t3 = Trade(
        id="t3",
        order_id="o3",
        symbol="CCM$",
        action=OrderAction.BUY,
        quantity=1,
        entry_time=datetime(2023, 1, 3, 10, 0, tzinfo=timezone.utc),
        entry_price=10.0,
    )
    t4 = Trade(
        id="t4",
        order_id="o4",
        symbol="CCM$",
        action=OrderAction.BUY,
        quantity=1,
        entry_time=datetime(2023, 1, 4, 10, 0, tzinfo=timezone.utc),
        entry_price=10.0,
    )

    registry.register_trade(t1)
    registry.register_trade(t2)
    registry.register_trade(t3)
    registry.register_trade(t4)

    # Close trades with deterministic exit times and prices to get custom PnL
    # (We bypass close_trade PnL formula and directly set PnL for simplicity and transparency)
    registry.close_trade(
        "t1", datetime(2023, 1, 1, 12, 0, tzinfo=timezone.utc), exit_price=12.0
    )
    t1.pnl = 2000.0

    registry.close_trade(
        "t2", datetime(2023, 1, 2, 12, 0, tzinfo=timezone.utc), exit_price=8.5
    )
    t2.pnl = -1500.0

    registry.close_trade(
        "t3", datetime(2023, 1, 3, 12, 0, tzinfo=timezone.utc), exit_price=9.5
    )
    t3.pnl = -500.0

    registry.close_trade(
        "t4", datetime(2023, 1, 4, 12, 0, tzinfo=timezone.utc), exit_price=13.0
    )
    t4.pnl = 3000.0

    metrics = registry.get_performance_metrics(initial_capital=10000.0)

    # Verification
    # Initial = 10,000
    # T1 exits -> Equity = 12,000, Peak = 12,000, Drawdown = 0
    # T2 exits -> Equity = 10,500, Peak = 12,000, Drawdown = 1,500 (12.5%)
    # T3 exits -> Equity = 10,000, Peak = 12,000, Drawdown = 2,000 (16.67%)
    # T4 exits -> Equity = 13,000, Peak = 13,000, Drawdown = 0

    assert metrics["total_trades"] == 4
    assert metrics["winning_trades"] == 2
    assert metrics["losing_trades"] == 2
    assert metrics["win_rate"] == 0.5
    assert metrics["total_pnl"] == 3000.0

    # Drawdowns
    assert metrics["max_drawdown_value"] == 2000.0
    assert metrics["max_drawdown_pct"] == pytest.approx(2000.0 / 12000.0)

    # Ratios
    # Gross Profit = 5000.0, Gross Loss = -2000.0
    assert metrics["profit_factor"] == pytest.approx(5000.0 / 2000.0)
    # Recovery Factor = 3000.0 / 2000.0 = 1.5
    assert metrics["recovery_factor"] == 1.5
    # Expectancy = 3000.0 / 4 = 750.0
    assert metrics["expectancy"] == 750.0

    # Streaks
    # T1 (+), T2 (-), T3 (-), T4 (+) -> Consecutive Wins = 1, Consecutive Losses = 2
    assert metrics["max_consecutive_wins"] == 1
    assert metrics["max_consecutive_losses"] == 2
