from typing import Any

from q_backend.backtesting.moving_averages import normalize_ma_type
from q_backend.backtesting.strategy import MACrossoverStrategy
from q_backend.backtesting.strategy_registry import StrategyParamSpec, register_strategy

MA_TYPE_CHOICES = sorted(["sma", "ema", "wma", "smma", "hma"])


def _build_ma_crossover(params: dict[str, Any], symbol: str) -> MACrossoverStrategy:
    return MACrossoverStrategy(
        short_period=int(params["short_period"]),
        long_period=int(params["long_period"]),
        threshold=float(params["threshold"]),
        short_ma_type=normalize_ma_type(params["short_ma_type"]),
        long_ma_type=normalize_ma_type(params["long_ma_type"]),
        symbol=symbol,
    )


register_strategy(
    name="MACrossover",
    label="MA Crossover",
    description="Short/long moving-average crossover.",
    params=[
        StrategyParamSpec(
            name="short_period",
            label="Short Period",
            type="int",
            default=50,
            min=2,
            max=400,
            step=1,
        ),
        StrategyParamSpec(
            name="long_period",
            label="Long Period",
            type="int",
            default=200,
            min=2,
            max=400,
            step=1,
        ),
        StrategyParamSpec(
            name="short_ma_type",
            label="Short MA Type",
            type="categorical",
            default="sma",
            choices=MA_TYPE_CHOICES,
        ),
        StrategyParamSpec(
            name="long_ma_type",
            label="Long MA Type",
            type="categorical",
            default="sma",
            choices=MA_TYPE_CHOICES,
        ),
        StrategyParamSpec(
            name="threshold",
            label="Threshold",
            type="float",
            default=0.0,
            min=0.0,
            max=100.0,
            step=0.01,
        ),
    ],
    build=_build_ma_crossover,
    strategy_class=MACrossoverStrategy,
)
