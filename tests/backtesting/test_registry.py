import pytest
from datetime import datetime, timezone
from q_backend.backtesting.models import Order, Trade, OrderAction, OrderType, OrderStatus, TradeStatus
from q_backend.backtesting.registry import TradeRegistry

def test_register_order():
    registry = TradeRegistry()
    order = Order(
        id="order-1",
        symbol="EURUSD",
        action=OrderAction.BUY,
        order_type=OrderType.MARKET,
        quantity=1.0
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
        commission=5.0
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
        id="t1", order_id="o1", symbol="EURUSD", action=OrderAction.BUY,
        quantity=100000, entry_time=datetime.now(timezone.utc), entry_price=1.1000
    )
    trade2 = Trade(
        id="t2", order_id="o2", symbol="EURUSD", action=OrderAction.SELL,
        quantity=100000, entry_time=datetime.now(timezone.utc), entry_price=1.1100
    )
    
    registry.register_trade(trade1)
    registry.register_trade(trade2)
    
    registry.close_trade("t1", datetime.now(timezone.utc), exit_price=1.1050) # +500 PnL
    registry.close_trade("t2", datetime.now(timezone.utc), exit_price=1.1080) # +200 PnL
    
    metrics = registry.get_performance_metrics()
    
    assert metrics["total_trades"] == 2
    assert metrics["total_pnl"] == pytest.approx(700.0)
    assert metrics["win_rate"] == 1.0
    assert metrics["winning_trades"] == 2
    assert metrics["losing_trades"] == 0
