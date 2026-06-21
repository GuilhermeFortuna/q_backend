import pytest
import pandas as pd
from datetime import datetime, timezone
from q_backend.backtesting.models import Signal, SignalAction, Trade
from q_backend.backtesting.exit_strategy import ExitStrategy
from q_backend.backtesting.engine import BacktestEngine
from q_backend.backtesting.strategy import TradingStrategy
from q_backend.backtesting.position_sizing import FixedQuantitySizer
from q_backend.backtesting.factory import build_strategy
from q_backend.backtesting.strategy_registry import (
    register_strategy,
    get_registered_strategy,
    list_registered_strategies,
    default_params_for,
    _STRATEGY_REGISTRY,
    _CUSTOM_STRATEGIES,
)
from q_backend.backtesting.custom_strategy_store import (
    load_custom_strategies,
    save_custom_strategies,
    custom_strategies_path,
)

# 1. Unit Tests for ExitStrategy class directly

def test_fixed_stop_loss_long():
    # Long trade entered at 100. Stop loss at 2% = 98.
    exit_strat = ExitStrategy(stop_loss_pct=0.02)
    trade = Trade(
        id="t1",
        order_id="o1",
        symbol="TEST",
        action=SignalAction.BUY,
        quantity=1.0,
        entry_time=datetime.now(timezone.utc),
        entry_price=100.0,
    )
    
    # Price is 99 (SL not hit)
    current_data = pd.Series({"close": 99.0, "high": 99.5, "low": 98.5})
    exits = exit_strat.check_exits([trade], current_data)
    assert len(exits) == 0
    
    # Price low is 97.9 (SL hit!)
    current_data_hit = pd.Series({"close": 98.0, "high": 99.0, "low": 97.9})
    exits_hit = exit_strat.check_exits([trade], current_data_hit)
    assert len(exits_hit) == 1
    assert exits_hit[0].action == SignalAction.CLOSE


def test_fixed_take_profit_long():
    # Long trade entered at 100. Take profit at 5% = 105.
    exit_strat = ExitStrategy(take_profit_pct=0.05)
    trade = Trade(
        id="t1",
        order_id="o1",
        symbol="TEST",
        action=SignalAction.BUY,
        quantity=1.0,
        entry_time=datetime.now(timezone.utc),
        entry_price=100.0,
    )
    
    # Price high is 104 (TP not hit)
    current_data = pd.Series({"close": 103.0, "high": 104.0, "low": 102.0})
    exits = exit_strat.check_exits([trade], current_data)
    assert len(exits) == 0
    
    # Price high is 106 (TP hit!)
    current_data_hit = pd.Series({"close": 104.0, "high": 106.0, "low": 103.0})
    exits_hit = exit_strat.check_exits([trade], current_data_hit)
    assert len(exits_hit) == 1
    assert exits_hit[0].action == SignalAction.CLOSE


def test_trailing_stop_long():
    # Long trade entered at 100. Trailing stop 2%. Initial SL = 98.
    exit_strat = ExitStrategy(trailing_stop_pct=0.02)
    trade = Trade(
        id="t1",
        order_id="o1",
        symbol="TEST",
        action=SignalAction.BUY,
        quantity=1.0,
        entry_time=datetime.now(timezone.utc),
        entry_price=100.0,
    )
    
    # Candle 1: High reaches 102. New SL is 102 * 0.98 = 99.96. Low is 100.5 (SL not hit).
    current_data_1 = pd.Series({"close": 101.5, "high": 102.0, "low": 100.5})
    exits_1 = exit_strat.check_exits([trade], current_data_1)
    assert len(exits_1) == 0
    assert exit_strat._extreme_prices[trade.id] == 102.0
    
    # Candle 2: High reaches 105. New SL is 105 * 0.98 = 102.9. Low is 103.5 (SL not hit).
    current_data_2 = pd.Series({"close": 104.0, "high": 105.0, "low": 103.5})
    exits_2 = exit_strat.check_exits([trade], current_data_2)
    assert len(exits_2) == 0
    assert exit_strat._extreme_prices[trade.id] == 105.0
    
    # Candle 3: High is 104, Low is 102.5. (SL at 102.9 is hit!)
    current_data_3 = pd.Series({"close": 103.0, "high": 104.0, "low": 102.5})
    exits_3 = exit_strat.check_exits([trade], current_data_3)
    assert len(exits_3) == 1
    assert exits_3[0].action == SignalAction.CLOSE


def test_atr_exits_long():
    # ATR is 2.0. Stop Loss at 1.5 * ATR = 3.0 below entry (SL = 97).
    # Take Profit at 3.0 * ATR = 6.0 above entry (TP = 106).
    exit_strat = ExitStrategy(stop_loss_atr=1.5, take_profit_atr=3.0, atr_period=14)
    trade = Trade(
        id="t1",
        order_id="o1",
        symbol="TEST",
        action=SignalAction.BUY,
        quantity=1.0,
        entry_time=datetime.now(timezone.utc),
        entry_price=100.0,
    )
    
    # Candle 1: ATR is 2.0. Low is 98.0, High is 103.0. No exit.
    current_data_1 = pd.Series({"close": 101.0, "high": 103.0, "low": 98.0, "atr_14": 2.0})
    exits_1 = exit_strat.check_exits([trade], current_data_1)
    assert len(exits_1) == 0
    
    # Candle 2: ATR is 2.0. Low is 96.5 (SL hit!)
    current_data_2 = pd.Series({"close": 98.0, "high": 100.0, "low": 96.5, "atr_14": 2.0})
    exits_2 = exit_strat.check_exits([trade], current_data_2)
    assert len(exits_2) == 1
    assert exits_2[0].action == SignalAction.CLOSE


# 2. Integration Tests with Strategy Registry & custom strategies

def test_dynamic_custom_strategy_registration(tmp_path, monkeypatch):
    # Mock custom_strategies_path to point to a temp path
    mock_file = tmp_path / "custom_strategies.json"
    monkeypatch.setattr("q_backend.backtesting.custom_strategy_store.custom_strategies_path", lambda: mock_file)
    
    # Initially no custom strategies
    assert mock_file.is_file() is False
    
    # Save a custom strategy config
    custom_config = [
        {
            "name": "MyCustomMACrossover",
            "base_strategy": "MACrossover",
            "description": "MACrossover with custom short/long and trailing SL",
            "parameters": {
                "short_period": 10,
                "long_period": 30,
                "trailing_stop_pct": 0.015,
            }
        }
    ]
    
    save_custom_strategies(custom_config)
    
    # Load and register should run automatically when listing registered strategies
    all_strategies = list_registered_strategies()
    strategy_names = [s.name for s in all_strategies]
    assert "MyCustomMACrossover" in strategy_names
    
    # Check details of custom strategy spec
    spec = get_registered_strategy("MyCustomMACrossover")
    assert spec.info.name == "MyCustomMACrossover"
    assert spec.info.description == "MACrossover with custom short/long and trailing SL"
    
    # Verify that parameter defaults were overridden
    defaults = default_params_for("MyCustomMACrossover")
    assert defaults["short_period"] == 10
    assert defaults["long_period"] == 30
    assert defaults["trailing_stop_pct"] == 0.015
    # Standard MACrossover defaults should remain unchanged in its original entry
    orig_defaults = default_params_for("MACrossover")
    assert orig_defaults["short_period"] == 50
    assert orig_defaults["trailing_stop_pct"] == 0.0  # default disabled
    
    # Build it
    custom_strategy = build_strategy("MyCustomMACrossover", {}, "BTCUSDT")
    assert custom_strategy.parameters["short_period"] == 10
    assert custom_strategy.parameters["trailing_stop_pct"] == 0.015
    assert custom_strategy.exit_strategy.trailing_stop_pct == 0.015
