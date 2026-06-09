import pytest
import pandas as pd

from q_backend.backtesting.factory import build_strategy
from q_backend.backtesting.strategy import MACrossoverStrategy
from q_backend.backtesting.strategy_registry import (
    default_params_for,
    list_registered_strategies,
    merge_strategy_params,
)
from q_backend.backtesting.strategies.bollinger_reversion import BollingerReversionStrategy
from q_backend.backtesting.strategies.donchian_breakout import DonchianBreakoutStrategy
from q_backend.backtesting.strategies.macd import MACDStrategy
from q_backend.backtesting.strategies.rsi_mean_reversion import RSIMeanReversionStrategy


REGISTERED_NAMES = [
    "BollingerReversion",
    "DonchianBreakout",
    "MACD",
    "MACrossover",
    "RSIMeanReversion",
]


def test_all_strategies_registered():
    names = [info.name for info in list_registered_strategies()]
    assert names == sorted(REGISTERED_NAMES)


@pytest.mark.parametrize("name,expected_class", [
    ("MACrossover", MACrossoverStrategy),
    ("RSIMeanReversion", RSIMeanReversionStrategy),
    ("BollingerReversion", BollingerReversionStrategy),
    ("MACD", MACDStrategy),
    ("DonchianBreakout", DonchianBreakoutStrategy),
])
def test_build_strategy_dispatches(name, expected_class):
    strategy = build_strategy(name, {}, "TEST")
    assert isinstance(strategy, expected_class)


def test_build_strategy_ma_crossover_with_params():
    strategy = build_strategy(
        "MACrossover",
        {"short_period": 10, "long_period": 30, "short_ma_type": "ema"},
        "TEST",
    )
    assert isinstance(strategy, MACrossoverStrategy)
    assert strategy.parameters["short_period"] == 10
    assert strategy.short_ma_type == "ema"


def test_build_strategy_ma_crossover_defaults():
    strategy = build_strategy("MACrossover", {}, "TEST")
    defaults = default_params_for("MACrossover")
    assert strategy.short_period == defaults["short_period"] == 50
    assert strategy.long_period == defaults["long_period"] == 200
    assert strategy.threshold == defaults["threshold"] == 0.0
    assert strategy.short_ma_type == defaults["short_ma_type"] == "sma"
    assert strategy.long_ma_type == defaults["long_ma_type"] == "sma"


def test_merge_strategy_params_round_trips_macrossover_defaults():
    merged = merge_strategy_params("MACrossover", {})
    assert merged == default_params_for("MACrossover")


def test_build_strategy_unknown_raises():
    with pytest.raises(ValueError, match="Unknown strategy"):
        build_strategy("Unknown", {}, "TEST")
