import pytest
import pandas as pd
from typing import List
from q_backend.backtesting.models import Signal, SignalAction, Trade
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
    
def test_valid_strategy_implementation():
    """
    Test that a correctly implemented strategy can be instantiated and returns expected Signals.
    """
    class MovingAverageCrossover(TradingStrategy):
        def compute_indicators(self, data: pd.DataFrame) -> pd.DataFrame:
            data['ma_short'] = data['close'].rolling(10).mean()
            data['ma_long'] = data['close'].rolling(50).mean()
            return data

        def check_entry_conditions(self, current_data: pd.Series) -> List[Signal]:
            if current_data.get('ma_short', 0) > current_data.get('ma_long', 0):
                return [Signal(symbol=current_data.name or "UNKNOWN", action=SignalAction.BUY)]
            return []

        def check_exit_conditions(self, current_data: pd.Series, open_trades: List[Trade]) -> List[Signal]:
            # Simple exit logic: if short MA crosses below long MA, close all positions
            if current_data.get('ma_short', 0) < current_data.get('ma_long', 0):
                return [Signal(symbol=current_data.name or "UNKNOWN", action=SignalAction.CLOSE)]
            return []

    # Instantiate
    strategy = MovingAverageCrossover(short_window=10, long_window=50)
    assert strategy.parameters['short_window'] == 10
    
    # Test indicators
    df = pd.DataFrame({'close': [100.0] * 50})
    df = strategy.compute_indicators(df)
    assert 'ma_short' in df.columns
    assert 'ma_long' in df.columns

    # Test entry
    series_buy = pd.Series({'ma_short': 105, 'ma_long': 100}, name="AAPL")
    signals = strategy.check_entry_conditions(series_buy)
    assert len(signals) == 1
    assert signals[0].action == SignalAction.BUY
    assert signals[0].symbol == "AAPL"
    
    # Test exit
    series_exit = pd.Series({'ma_short': 95, 'ma_long': 100}, name="AAPL")
    exit_signals = strategy.check_exit_conditions(series_exit, [])
    assert len(exit_signals) == 1
    assert exit_signals[0].action == SignalAction.CLOSE
    assert exit_signals[0].symbol == "AAPL"
