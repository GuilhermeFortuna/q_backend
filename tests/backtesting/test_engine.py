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
        price = current_data.get("close", 0)
        # Buy if price is 100
        if price == 100.0:
            return [Signal(symbol="DUMMY", action=SignalAction.BUY)]
        return []

    def check_exit_conditions(
        self, current_data: pd.Series, open_trades: List[Trade]
    ) -> List[Signal]:
        price = current_data.get("close", 0)
        # Sell if price is 110
        if price == 110.0:
            return [Signal(symbol="DUMMY", action=SignalAction.CLOSE)]
        return []

    def get_chart_indicators(self):
        return []


@pytest.fixture
def dummy_data():
    """
    Creates 3 days of data with distinct open/close prices so tests can verify
    that fills happen at the NEXT bar's open (not the signalling bar's close).

    Day 1: close hits 100 (BUY signal) then 105
    Day 2: close 105 then 105
    Day 3: close hits 110 (SELL/CLOSE signal) then 115
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
    closes = [100.0, 105.0, 105.0, 105.0, 110.0, 115.0]
    opens = [99.0, 101.0, 104.0, 106.0, 108.0, 112.0]

    df = pd.DataFrame({"open": opens, "close": closes}, index=times)
    return df


def test_engine_sequential_swing_trade(dummy_data):
    """
    Sequential run keeps the trade open across days and closes on Day 3.

    The BUY signal fires on bar 0's close (price 100) and fills at bar 1's
    open (101.0). The CLOSE signal fires on bar 4's close (price 110) and
    fills at bar 5's open (112.0).
    """
    strategy = DummyStrategy()
    sizer = FixedQuantitySizer(quantity=1.0)
    engine = BacktestEngine(strategy, sizer, initial_capital=1000)

    registry = engine.run(dummy_data, parallel_mode=ParallelMode.SEQUENTIAL)

    assert len(registry.get_open_trades()) == 0
    assert len(registry.get_closed_trades()) == 1

    closed_trade = registry.get_closed_trades()[0]
    assert closed_trade.entry_price == 101.0
    assert closed_trade.exit_price == 112.0
    assert closed_trade.pnl == pytest.approx(11.0)


def test_engine_parallel_day_trade(dummy_data):
    """
    A DAY_TRADE parallel run forces the trade closed at the end of Day 1.
    Day 2 does nothing.
    Day 3 does nothing because the price is 110 (exit condition) but no trade is open.

    The BUY signal fires on Day 1 bar 0's close (100) and fills at bar 1's
    open (101.0); it is then force-closed at Day 1's final close (105.0).
    """
    strategy = DummyStrategy()
    sizer = FixedQuantitySizer(quantity=1.0)
    engine = BacktestEngine(strategy, sizer, initial_capital=1000)

    registry = engine.run(dummy_data, parallel_mode=ParallelMode.DAY_TRADE)

    assert len(registry.get_open_trades()) == 0

    # One trade from Day 1: entered at bar 1's open (101.0), force-closed at
    # Day 1's last close (105.0).
    closed_trades = registry.get_closed_trades()
    assert len(closed_trades) == 1

    trade = closed_trades[0]
    assert trade.entry_price == 101.0
    assert trade.exit_price == 105.0
    assert trade.pnl == pytest.approx(4.0)


class AlwaysShortStrategy(TradingStrategy):
    """Emits a SELL entry on every bar and never exits.

    Models the screenshot scenario: a strategy whose entry condition keeps
    firing in the same direction. The engine must NOT pyramid into a larger
    position than the configured size.
    """

    def compute_indicators(self, data: pd.DataFrame) -> pd.DataFrame:
        return data

    def check_entry_conditions(self, current_data: pd.Series) -> List[Signal]:
        return [Signal(symbol="DUMMY", action=SignalAction.SELL)]

    def check_exit_conditions(
        self, current_data: pd.Series, open_trades: List[Trade]
    ) -> List[Signal]:
        return []

    def get_chart_indicators(self):
        return []


@pytest.mark.parametrize("max_quantity", [1.0, 2.0, 5.0])
def test_engine_caps_position_at_risk_model_size(max_quantity):
    """
    Repeated same-direction entry signals must not stack beyond the risk
    model's maximum position size. With Fixed Quantity = N and no exit, total
    open exposure equals N (held in the first position) no matter how many
    entry signals fire afterwards -- the cap is the configured quantity, not a
    hardcoded 1.
    """
    base = datetime(2023, 1, 1, 10, 0, tzinfo=timezone.utc)
    times = [base + timedelta(hours=i) for i in range(6)]
    df = pd.DataFrame(
        {
            "open": [10.0, 11.0, 12.0, 13.0, 14.0, 15.0],
            "close": [10.0, 11.0, 12.0, 13.0, 14.0, 15.0],
        },
        index=times,
    )

    strategy = AlwaysShortStrategy()
    sizer = FixedQuantitySizer(quantity=max_quantity)
    engine = BacktestEngine(strategy, sizer, initial_capital=1_000_000)

    registry = engine.run(df, parallel_mode=ParallelMode.SEQUENTIAL)

    open_trades = registry.get_open_trades()
    total_exposure = sum(t.quantity for t in open_trades)
    # Open exposure is capped at the configured quantity, never more.
    assert total_exposure == pytest.approx(max_quantity)
    # A single Fixed Quantity order already fills the cap, so later signals
    # add nothing.
    assert len(registry.get_all_trades()) == 1
    # Filled at bar 1's open after the bar-0 signal.
    assert open_trades[0].entry_price == 11.0


def test_engine_with_macrossover():
    """
    Tests the BacktestEngine integrated with the MACrossoverStrategy.
    """
    from q_backend.backtesting import (
        BacktestEngine,
        MACrossoverStrategy,
        FixedQuantitySizer,
    )

    # 2 period short MA, 4 period long MA, 1.0 threshold
    strategy = MACrossoverStrategy(
        short_period=2, long_period=4, threshold=1.0, symbol="BTCUSDT"
    )
    sizer = FixedQuantitySizer(quantity=1.0)
    engine = BacktestEngine(strategy, sizer, initial_capital=1000)

    # Close drives the crossover; open drives the fill price.
    #   close index 5 (16.0) -> BUY signal, fills at index 6's open (11.0)
    #   close index 7 (4.0)  -> CLOSE long + SELL, both fill at index 8's open (5.0)
    # Trailing bars (8, 9) exist so those signals actually execute.
    base = datetime(2023, 1, 1, 10, 0, tzinfo=timezone.utc)
    times = [base + timedelta(hours=i) for i in range(10)]
    closes = [10.0, 10.0, 10.0, 10.0, 13.0, 16.0, 10.0, 4.0, 4.0, 4.0]
    opens = [10.0, 10.0, 10.0, 10.0, 12.0, 15.0, 11.0, 9.0, 5.0, 4.0]
    df = pd.DataFrame({"open": opens, "close": closes}, index=times)

    registry = engine.run(df)

    # Closed BUY: entered at index 6's open (11.0), closed at index 8's open (5.0).
    # Open SELL: entered at index 8's open (5.0).
    closed_trades = registry.get_closed_trades()
    open_trades = registry.get_open_trades()

    assert len(closed_trades) == 1
    assert closed_trades[0].action == "BUY"
    assert closed_trades[0].entry_price == 11.0
    assert closed_trades[0].exit_price == 5.0
    # PnL = (5.0 - 11.0) * 1 = -6.0
    assert closed_trades[0].pnl == pytest.approx(-6.0)

    assert len(open_trades) == 1
    assert open_trades[0].action == "SELL"
    assert open_trades[0].entry_price == 5.0


def test_engine_fills_at_next_bar_open_not_signal_bar_close():
    """
    Regression guard for look-ahead bias: a signal detected on a closed bar
    must fill at the *next* bar's open, never at the signalling bar's own
    close. The close of the signal bar (110.0) and the next open (200.0) are
    deliberately far apart so a same-bar-close fill would be unmistakable.
    """
    base = datetime(2023, 1, 1, 10, 0, tzinfo=timezone.utc)
    times = [base + timedelta(hours=i) for i in range(3)]
    df = pd.DataFrame(
        {
            "open": [50.0, 200.0, 300.0],  # bar 1 open is the expected entry fill
            "close": [100.0, 150.0, 150.0],  # bar 0 close (100) triggers BUY
        },
        index=times,
    )

    strategy = DummyStrategy()
    sizer = FixedQuantitySizer(quantity=1.0)
    engine = BacktestEngine(strategy, sizer, initial_capital=1000)

    registry = engine.run(df, parallel_mode=ParallelMode.SEQUENTIAL)

    trades = registry.get_all_trades()
    assert len(trades) == 1
    # Fills at bar 1's open (200.0), NOT bar 0's close (100.0).
    assert trades[0].entry_price == 200.0


def test_engine_with_point_value():
    """
    Tests that the BacktestEngine correctly applies custom symbol point values
    to compute multiplied trade PnL (e.g. CCM$ Corn futures contract size multiplier).
    """
    from q_backend.backtesting import (
        BacktestEngine,
        MACrossoverStrategy,
        FixedQuantitySizer,
    )

    # Define custom point_values mapping CCM$ to 450.0
    point_values = {"CCM$": 450.0}
    strategy = MACrossoverStrategy(
        short_period=2, long_period=4, threshold=1.0, symbol="CCM$"
    )
    sizer = FixedQuantitySizer(quantity=2.0)  # 2 contracts
    engine = BacktestEngine(
        strategy, sizer, initial_capital=100000, point_values=point_values
    )

    base = datetime(2023, 1, 1, 10, 0, tzinfo=timezone.utc)
    times = [base + timedelta(hours=i) for i in range(10)]
    closes = [10.0, 10.0, 10.0, 10.0, 13.0, 16.0, 10.0, 4.0, 4.0, 4.0]
    opens = [10.0, 10.0, 10.0, 10.0, 12.0, 15.0, 11.0, 9.0, 5.0, 4.0]
    df = pd.DataFrame({"open": opens, "close": closes}, index=times)

    registry = engine.run(df)
    closed_trades = registry.get_closed_trades()

    assert len(closed_trades) == 1
    trade = closed_trades[0]
    assert trade.symbol == "CCM$"
    assert trade.point_value == 450.0
    assert trade.quantity == 2.0
    # Entered at index 6's open (11.0), closed at index 8's open (5.0).
    assert trade.entry_price == 11.0
    assert trade.exit_price == 5.0
    # PnL = (Exit - Entry) * Quantity * Point Value
    # PnL = (5.0 - 11.0) * 2.0 * 450.0 = -6.0 * 900.0 = -5400.0
    assert trade.pnl == pytest.approx(-5400.0)


def test_engine_sequential_day_trade(dummy_data):
    """
    A SEQUENTIAL run with day_trade=True forces the trade closed at the end of each day
    and clears pending signals so they do not carry over to the next day's opening bar.
    """
    strategy = DummyStrategy()
    sizer = FixedQuantitySizer(quantity=1.0)
    engine = BacktestEngine(strategy, sizer, initial_capital=1000, day_trade=True)

    registry = engine.run(dummy_data, parallel_mode=ParallelMode.SEQUENTIAL)

    assert len(registry.get_open_trades()) == 0

    # One trade from Day 1: entered at bar 1's open (101.0), force-closed at
    # Day 1's last close (105.0) because day_trade=True.
    closed_trades = registry.get_closed_trades()
    assert len(closed_trades) == 1

    trade = closed_trades[0]
    assert trade.entry_price == 101.0
    assert trade.exit_price == 105.0
    assert trade.pnl == pytest.approx(4.0)


def test_engine_day_trade_hours():
    """
    Verifies that in day trade mode, entries are only scanned during the entry window,
    and open positions are force-closed exactly when the close time is reached.
    """
    # DummyStrategy buys when close is 100.
    strategy = DummyStrategy()
    sizer = FixedQuantitySizer(quantity=1.0)
    
    # Configure: entries between 09:30 and 15:00, force close at 15:30.
    engine = BacktestEngine(
        strategy, 
        sizer, 
        initial_capital=1000, 
        day_trade=True,
        day_trade_start_time="09:30",
        day_trade_end_time="15:00",
        day_trade_close_time="15:30"
    )

    base = datetime(2023, 1, 1, 0, 0, tzinfo=timezone.utc)
    
    times = [
        base + timedelta(hours=9),       # 09:00:00 (outside entry window)
        base + timedelta(hours=10),      # 10:00:00 (inside entry window)
        base + timedelta(hours=11),      # 11:00:00 (inside entry window)
        base + timedelta(hours=15, minutes=30),  # 15:30:00 (force-close time)
        base + timedelta(hours=16),      # 16:00:00 (after force-close)
    ]
    
    # Bar 0 (09:00): close=100. Triggers BUY, but since 09:00 < 09:30, it is ignored!
    # Bar 1 (10:00): close=100. Triggers BUY (inside entry window 09:30 - 15:00).
    # Bar 2 (11:00): open=101. Fills BUY. Close=102.
    # Bar 3 (15:30): open=104. Since 15:30 >= 15:30 (force-close time), it is force-closed at open (104.0).
    closes = [100.0, 100.0, 102.0, 105.0, 105.0]
    opens = [99.0, 99.0, 101.0, 104.0, 105.0]
    
    df = pd.DataFrame({"open": opens, "close": closes}, index=times)
    
    registry = engine.run(df)
    
    closed_trades = registry.get_closed_trades()
    assert len(closed_trades) == 1
    
    trade = closed_trades[0]
    assert trade.entry_time == times[2]  # Filled at Bar 2's open
    assert trade.entry_price == 101.0
    assert trade.exit_time == times[3]   # Force closed at Bar 3's open
    assert trade.exit_price == 104.0     # Fills at force-close time open (104.0)
    assert trade.pnl == pytest.approx(3.0)
