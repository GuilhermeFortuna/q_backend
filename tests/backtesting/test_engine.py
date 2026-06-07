import pytest
import pandas as pd
from datetime import datetime, timedelta, timezone
from typing import List

from q_backend.backtesting.models import Signal, SignalAction, Trade
from q_backend.backtesting.strategy import TradingStrategy
from q_backend.backtesting.position_sizing import FixedQuantitySizer
from q_backend.backtesting.engine import BacktestEngine, ParallelMode

class DummyStrategy(TradingStrategy):
    def compute_indicators(self, data: pd.DataFrame) -> pd.DataFrame:
        # Just return as is
        return data

    def check_entry_conditions(self, current_data: pd.Series) -> List[Signal]:
        price = current_data.get('close', 0)
        # Buy if price is 100
        if price == 100.0:
            return [Signal(symbol="DUMMY", action=SignalAction.BUY)]
        return []

    def check_exit_conditions(self, current_data: pd.Series, open_trades: List[Trade]) -> List[Signal]:
        price = current_data.get('close', 0)
        # Sell if price is 110
        if price == 110.0:
            return [Signal(symbol="DUMMY", action=SignalAction.CLOSE)]
        return []

    def get_chart_indicators(self):
        return []

@pytest.fixture
def dummy_data():
    """
    Creates 3 days of data.
    Day 1: Hits 100 (BUY) then 105
    Day 2: Hits 105 then 105
    Day 3: Hits 110 (SELL) then 115
    """
    base = datetime(2023, 1, 1, 10, 0, tzinfo=timezone.utc)
    
    times = [
        base, 
        base + timedelta(hours=1),
        base + timedelta(days=1),
        base + timedelta(days=1, hours=1),
        base + timedelta(days=2),
        base + timedelta(days=2, hours=1),
    ]
    prices = [100.0, 105.0, 105.0, 105.0, 110.0, 115.0]
    
    df = pd.DataFrame({'close': prices}, index=times)
    return df

def test_engine_sequential_swing_trade(dummy_data):
    """
    Tests that a sequential run keeps the trade open across days and closes on Day 3.
    """
    strategy = DummyStrategy()
    sizer = FixedQuantitySizer(quantity=1.0)
    engine = BacktestEngine(strategy, sizer, initial_capital=1000)
    
    registry = engine.run(dummy_data, parallel_mode=ParallelMode.SEQUENTIAL)
    
    assert len(registry.get_open_trades()) == 0
    assert len(registry.get_closed_trades()) == 1
    
    closed_trade = registry.get_closed_trades()[0]
    assert closed_trade.entry_price == 100.0
    assert closed_trade.exit_price == 110.0
    assert closed_trade.pnl == 10.0

def test_engine_parallel_day_trade(dummy_data):
    """
    Tests that a DAY_TRADE parallel run forces the trade closed at the end of Day 1.
    Day 2 does nothing.
    Day 3 does nothing because the price is 110 (which is exit condition, but no trade is open).
    """
    strategy = DummyStrategy()
    sizer = FixedQuantitySizer(quantity=1.0)
    engine = BacktestEngine(strategy, sizer, initial_capital=1000)
    
    registry = engine.run(dummy_data, parallel_mode=ParallelMode.DAY_TRADE)
    
    assert len(registry.get_open_trades()) == 0
    
    # We should have one trade from Day 1, forcefully closed at the last price of Day 1 (105.0)
    closed_trades = registry.get_closed_trades()
    assert len(closed_trades) == 1
    
    trade = closed_trades[0]
    assert trade.entry_price == 100.0
    assert trade.exit_price == 105.0
    assert trade.pnl == 5.0


def test_engine_with_macrossover():
    """
    Tests the BacktestEngine integrated with the MACrossoverStrategy.
    """
    from q_backend.backtesting import BacktestEngine, MACrossoverStrategy, FixedQuantitySizer
    
    # 2 period short MA, 4 period long MA, 1.0 threshold
    strategy = MACrossoverStrategy(short_period=2, long_period=4, threshold=1.0, symbol="BTCUSDT")
    sizer = FixedQuantitySizer(quantity=1.0)
    engine = BacktestEngine(strategy, sizer, initial_capital=1000)
    
    # Create price data where:
    # index 5 (16.0) triggers BUY
    # index 7 (4.0) triggers CLOSE of BUY and triggers SELL
    base = datetime(2023, 1, 1, 10, 0, tzinfo=timezone.utc)
    times = [base + timedelta(hours=i) for i in range(8)]
    prices = [10.0, 10.0, 10.0, 10.0, 13.0, 16.0, 10.0, 4.0]
    df = pd.DataFrame({'close': prices}, index=times)
    
    registry = engine.run(df)
    
    # We should have a closed BUY trade (opened at 16.0 at index 5, closed at 4.0 at index 7)
    # And an open SELL trade (opened at 4.0 at index 7)
    closed_trades = registry.get_closed_trades()
    open_trades = registry.get_open_trades()
    
    assert len(closed_trades) == 1
    assert closed_trades[0].action == "BUY"
    assert closed_trades[0].entry_price == 16.0
    assert closed_trades[0].exit_price == 4.0
    # PnL = (4.0 - 16.0) * 1 = -12.0
    assert closed_trades[0].pnl == -12.0
    
    assert len(open_trades) == 1
    assert open_trades[0].action == "SELL"
    assert open_trades[0].entry_price == 4.0


def test_engine_with_point_value():
    """
    Tests that the BacktestEngine correctly applies custom symbol point values
    to compute multiplied trade PnL (e.g. CCM$ Corn futures contract size multiplier).
    """
    from q_backend.backtesting import BacktestEngine, MACrossoverStrategy, FixedQuantitySizer
    
    # Define custom point_values mapping CCM$ to 450.0
    point_values = {"CCM$": 450.0}
    strategy = MACrossoverStrategy(short_period=2, long_period=4, threshold=1.0, symbol="CCM$")
    sizer = FixedQuantitySizer(quantity=2.0) # 2 contracts
    engine = BacktestEngine(strategy, sizer, initial_capital=100000, point_values=point_values)
    
    base = datetime(2023, 1, 1, 10, 0, tzinfo=timezone.utc)
    times = [base + timedelta(hours=i) for i in range(8)]
    prices = [10.0, 10.0, 10.0, 10.0, 13.0, 16.0, 10.0, 4.0]
    df = pd.DataFrame({'close': prices}, index=times)
    
    registry = engine.run(df)
    closed_trades = registry.get_closed_trades()
    
    assert len(closed_trades) == 1
    trade = closed_trades[0]
    assert trade.symbol == "CCM$"
    assert trade.point_value == 450.0
    assert trade.quantity == 2.0
    assert trade.entry_price == 16.0
    assert trade.exit_price == 4.0
    # PnL = (Exit - Entry) * Quantity * Point Value
    # PnL = (4.0 - 16.0) * 2.0 * 450.0 = -12.0 * 900.0 = -10800.0
    assert trade.pnl == -10800.0


