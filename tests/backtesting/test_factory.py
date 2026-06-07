import pytest

from q_backend.backtesting.factory import build_strategy
from q_backend.backtesting.strategy import MACrossoverStrategy


def test_build_strategy_ma_crossover():
    strategy = build_strategy(
        "MACrossover",
        {"short_period": 10, "long_period": 30, "short_ma_type": "ema"},
        "TEST",
    )
    assert isinstance(strategy, MACrossoverStrategy)
    assert strategy.parameters["short_period"] == 10


def test_build_strategy_unknown_raises():
    with pytest.raises(ValueError, match="Unknown strategy"):
        build_strategy("Unknown", {}, "TEST")
