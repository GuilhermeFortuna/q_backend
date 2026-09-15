import pytest
import pandas as pd
from q_backend.backtesting.signal_columns import (
    SIGNAL_ENTRY,
    SIGNAL_EXIT_LONG,
    SIGNAL_EXIT_SHORT,
)
from q_backend.backtesting.strategy import TradingStrategy


def test_strategy_abstract_methods_enforced():
    """
    Ensure that a strategy missing required methods cannot be instantiated.
    """

    class IncompleteStrategy(TradingStrategy):
        pass

    with pytest.raises(TypeError) as excinfo:
        IncompleteStrategy()

    assert "Can't instantiate abstract class IncompleteStrategy" in str(excinfo.value)


def test_columnar_strategy_only_needs_indicators_and_chart_specs():
    """A subclass implementing only compute_indicators and get_chart_indicators is concrete."""

    class ColumnarOnlyStrategy(TradingStrategy):
        def compute_indicators(self, data: pd.DataFrame) -> pd.DataFrame:
            return data

        def get_chart_indicators(self):
            return []

    strategy = ColumnarOnlyStrategy(foo=1)
    assert strategy.parameters["foo"] == 1


def test_valid_strategy_implementation():
    """
    Test that a correctly implemented strategy can be instantiated and computes indicators.
    """

    class MovingAverageCrossover(TradingStrategy):
        def compute_indicators(self, data: pd.DataFrame) -> pd.DataFrame:
            data["ma_short"] = data["close"].rolling(10).mean()
            data["ma_long"] = data["close"].rolling(50).mean()
            return data

        def get_chart_indicators(self):
            return []

    strategy = MovingAverageCrossover(short_window=10, long_window=50)
    assert strategy.parameters["short_window"] == 10

    df = pd.DataFrame({"close": [100.0] * 50})
    df = strategy.compute_indicators(df)
    assert "ma_short" in df.columns
    assert "ma_long" in df.columns


def test_macrossover_strategy():
    """
    Test that the MACrossoverStrategy computes moving averages, delta,
    and writes entry/exit signal columns correctly based on a threshold.
    """
    from q_backend.backtesting import MACrossoverStrategy

    # Initialize strategy with short_period=2, long_period=4, threshold=1.0, symbol="BTCUSDT"
    strategy = MACrossoverStrategy(short_period=2, long_period=4, threshold=1.0, symbol="BTCUSDT")
    assert strategy.parameters["short_period"] == 2
    assert strategy.parameters["long_period"] == 4
    assert strategy.parameters["threshold"] == 1.0

    # Create price data to test indicators
    # Prices:
    # 1. 10.0 (ma_short=NaN, ma_long=NaN)
    # 2. 10.0 (ma_short=10.0, ma_long=NaN)
    # 3. 10.0 (ma_short=10.0, ma_long=NaN)
    # 4. 10.0 (ma_short=10.0, ma_long=10.0, delta=0.0)
    # 5. 13.0 (ma_short=(10+13)/2 = 11.5, ma_long=(10+10+10+13)/4 = 10.75, delta=0.75)
    # 6. 16.0 (ma_short=(13+16)/2 = 14.5, ma_long=(10+10+13+16)/4 = 12.25, delta=2.25) -> crosses above threshold 1.0
    # 7. 10.0 (ma_short=(16+10)/2 = 13.0, ma_long=(10+13+16+10)/4 = 12.25, delta=0.75)
    # 8. 4.0  (ma_short=(10+4)/2  = 7.0,  ma_long=(13+16+10+4)/4  = 10.75, delta=-3.75) -> crosses below -1.0
    prices = [10.0, 10.0, 10.0, 10.0, 13.0, 16.0, 10.0, 4.0]
    df = pd.DataFrame({"close": prices})

    df_with_indicators = strategy.compute_indicators(df)

    # Check that columns exist
    for col in [
        "ma_short",
        "ma_long",
        "delta",
        "prev_delta",
        "buy_signal",
        "sell_signal",
        SIGNAL_ENTRY,
        SIGNAL_EXIT_LONG,
        SIGNAL_EXIT_SHORT,
    ]:
        assert col in df_with_indicators.columns

    # Check values on candle index 5 (6th price: 16.0)
    # delta should be 14.5 - 12.25 = 2.25. prev_delta (from index 4) should be 0.75.
    # Since delta > 1.0 and prev_delta <= 1.0, buy_signal should be True.
    row_5 = df_with_indicators.iloc[5]
    assert row_5["delta"] == 2.25
    assert row_5["prev_delta"] == 0.75
    assert row_5["buy_signal"] == True
    assert row_5["sell_signal"] == False
    assert row_5[SIGNAL_ENTRY] == 1
    assert row_5[SIGNAL_EXIT_LONG] == False
    assert row_5[SIGNAL_EXIT_SHORT] == True

    # Check values on candle index 7 (8th price: 4.0)
    # delta should be 7.0 - 10.75 = -3.75. prev_delta (from index 6) should be 0.75.
    # Since delta < -1.0 and prev_delta >= -1.0, sell_signal should be True.
    row_7 = df_with_indicators.iloc[7]
    assert row_7["delta"] == -3.75
    assert row_7["prev_delta"] == 0.75
    assert row_7["buy_signal"] == False
    assert row_7["sell_signal"] == True
    assert row_7[SIGNAL_ENTRY] == -1
    assert row_7[SIGNAL_EXIT_LONG] == True
    assert row_7[SIGNAL_EXIT_SHORT] == False


def test_macrossover_strategy_with_ema_types():
    from q_backend.backtesting import MACrossoverStrategy

    strategy = MACrossoverStrategy(
        short_period=2,
        long_period=4,
        threshold=1.0,
        short_ma_type="ema",
        long_ma_type="wma",
        symbol="BTCUSDT",
    )

    assert strategy.short_ma_type == "ema"
    assert strategy.long_ma_type == "wma"

    prices = [10.0, 10.0, 10.0, 10.0, 13.0, 16.0, 10.0, 4.0]
    df = pd.DataFrame({"close": prices})
    df_with_indicators = strategy.compute_indicators(df)

    assert df_with_indicators["ma_short"].notna().any()
    assert df_with_indicators["ma_long"].notna().any()

    specs = strategy.get_chart_indicators()
    assert specs[0].label.startswith("EMA Short")
    assert specs[1].label.startswith("WMA Long")
