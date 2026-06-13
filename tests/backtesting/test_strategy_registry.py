import pytest
import pandas as pd

import q_backend.backtesting.tick.strategies  # noqa: F401
from q_backend.backtesting.factory import build_strategy
from q_backend.backtesting.tick.factory import build_tick_strategy
from q_backend.backtesting.tick.strategies.tick_ma_breakout import TickMaBreakoutStrategy
from q_backend.backtesting.strategy import MACrossoverStrategy
from q_backend.backtesting.strategy_registry import (
    _STRATEGY_REGISTRY,
    default_params_for,
    list_registered_strategies,
    merge_strategy_params,
    register_strategy,
    StrategyInfo,
    StrategyParamSpec,
)
from q_backend.backtesting.strategies.bollinger_reversion import BollingerReversionStrategy
from q_backend.backtesting.strategies.donchian_breakout import DonchianBreakoutStrategy
from q_backend.backtesting.strategies.fma import FMAStrategy
from q_backend.backtesting.strategies.macd import MACDStrategy
from q_backend.backtesting.strategies.rsi_mean_reversion import RSIMeanReversionStrategy
from q_backend.backtesting.strategies.trb import TRBStrategy
from q_backend.backtesting.strategies.vma import VMAStrategy


REGISTERED_NAMES = [
    "BollingerReversion",
    "CompositeStrategy",
    "DonchianBreakout",
    "FMA",
    "MACD",
    "MACrossover",
    "RSIMeanReversion",
    "TRB",
    "TickMaBreakout",
    "TSMOM",
    "VMA",
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
    ("VMA", VMAStrategy),
    ("FMA", FMAStrategy),
    ("TRB", TRBStrategy),
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


def test_tick_strategy_has_engine_field():
    from q_backend.backtesting.strategy_registry import get_registered_strategy

    tick_entry = get_registered_strategy("TickMaBreakout")
    candle_entry = get_registered_strategy("MACrossover")
    assert tick_entry.info.engine == "tick"
    assert candle_entry.info.engine == "candle"


def test_build_tick_strategy_dispatches():
    strategy = build_tick_strategy("TickMaBreakout", {}, "TEST")
    assert isinstance(strategy, TickMaBreakoutStrategy)


def test_strategy_info_additive_defaults():
    info = StrategyInfo(
        name="Test",
        label="Test",
        description="Test description",
        params=[],
    )
    assert info.category == "other"
    assert info.thesis == ""
    assert info.strong_in == ""
    assert info.weak_in == ""


def test_strategy_param_spec_additive_defaults():
    spec = StrategyParamSpec(
        name="x",
        label="X",
        type="int",
        default=1,
        min=0,
        max=10,
    )
    assert spec.hint is None


def test_register_strategy_additive_defaults():
    class _DummyStrategy:
        pass

    name = "_TestAdditiveStrategy"
    _STRATEGY_REGISTRY.pop(name, None)

    register_strategy(
        name=name,
        label="Test",
        description="Test description",
        params=[
            StrategyParamSpec(
                name="x",
                label="X",
                type="int",
                default=1,
                min=0,
                max=10,
            )
        ],
        build=lambda _params, _symbol: _DummyStrategy(),
        strategy_class=_DummyStrategy,
    )

    entry = _STRATEGY_REGISTRY[name]
    assert entry.info.category == "other"
    assert entry.info.thesis == ""
    assert entry.info.strong_in == ""
    assert entry.info.weak_in == ""
    assert entry.info.params[0].hint is None

    del _STRATEGY_REGISTRY[name]
