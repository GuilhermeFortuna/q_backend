from typing import Any

import numpy as np

from q_backend.backtesting.strategy import ChartIndicatorSpec
from q_backend.backtesting.strategy_registry import StrategyParamSpec, register_strategy
from q_backend.backtesting.tick.strategy import TickArrays, TickSignals, TickStrategy


def _rolling_sma(values: np.ndarray, period: int) -> np.ndarray:
    n = len(values)
    out = np.full(n, np.nan, dtype=np.float64)
    if period <= 0 or n < period:
        return out

    csum = np.cumsum(values)
    window_sum = csum[period - 1:] - np.concatenate(
        (np.array([0.0]), csum[: n - period])
    )
    out[period - 1:] = window_sum / period
    return out


class TickMaBreakoutStrategy(TickStrategy):
    """
    Tick-native moving-average breakout with fixed SL/TP distances in price units.
    """

    def __init__(
        self,
        short_period: int = 50,
        long_period: int = 200,
        threshold: float = 0.0,
        sl_points: float = 0.0,
        tp_points: float = 0.0,
        symbol: str = "TEST",
        **kwargs,
    ):
        super().__init__(
            short_period=short_period,
            long_period=long_period,
            threshold=threshold,
            sl_points=sl_points,
            tp_points=tp_points,
            symbol=symbol,
            **kwargs,
        )
        self.short_period = short_period
        self.long_period = long_period
        self.threshold = threshold
        self.sl_points = sl_points
        self.tp_points = tp_points
        self.symbol = symbol

    def compute_signals(self, ticks: TickArrays) -> TickSignals:
        price = ticks.last
        n = len(price)
        short_ma = _rolling_sma(price, self.short_period)
        long_ma = _rolling_sma(price, self.long_period)
        delta = short_ma - long_ma
        prev_delta = np.concatenate((np.array([np.nan]), delta[:-1]))

        direction = np.zeros(n, dtype=np.int8)
        buy = (delta > self.threshold) & (prev_delta <= self.threshold)
        sell = (delta < -self.threshold) & (prev_delta >= -self.threshold)
        direction[buy] = 1
        direction[sell] = -1

        sl = (
            np.full(n, self.sl_points, dtype=np.float64)
            if self.sl_points > 0.0
            else np.full(n, np.nan, dtype=np.float64)
        )
        tp = (
            np.full(n, self.tp_points, dtype=np.float64)
            if self.tp_points > 0.0
            else np.full(n, np.nan, dtype=np.float64)
        )

        return TickSignals(direction=direction, sl_points=sl, tp_points=tp)

    def get_chart_indicators(self) -> list[ChartIndicatorSpec]:
        return [
            ChartIndicatorSpec(
                key="ma_short",
                label=f"SMA Short ({self.short_period} ticks)",
                pane="price",
                color="#c9a227",
            ),
            ChartIndicatorSpec(
                key="ma_long",
                label=f"SMA Long ({self.long_period} ticks)",
                pane="price",
                color="#6eb5ff",
            ),
            ChartIndicatorSpec(
                key="delta",
                label="Delta",
                pane="oscillator",
                color="#c9a227",
            ),
        ]


def _build_tick_ma_breakout(params: dict[str, Any], symbol: str) -> TickMaBreakoutStrategy:
    return TickMaBreakoutStrategy(
        short_period=int(params["short_period"]),
        long_period=int(params["long_period"]),
        threshold=float(params["threshold"]),
        sl_points=float(params["sl_points"]),
        tp_points=float(params["tp_points"]),
        symbol=symbol,
    )


register_strategy(
    name="TickMaBreakout",
    label="Tick MA Breakout",
    description="Tick-native SMA breakout with optional stop/target distances.",
    engine="tick",
    params=[
        StrategyParamSpec(
            name="short_period",
            label="Short Period (ticks)",
            type="int",
            default=50,
            min=2,
            max=2000,
            step=1,
        ),
        StrategyParamSpec(
            name="long_period",
            label="Long Period (ticks)",
            type="int",
            default=200,
            min=2,
            max=5000,
            step=1,
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
        StrategyParamSpec(
            name="sl_points",
            label="Stop Loss (points)",
            type="float",
            default=0.0,
            min=0.0,
            max=1000.0,
            step=0.01,
        ),
        StrategyParamSpec(
            name="tp_points",
            label="Take Profit (points)",
            type="float",
            default=0.0,
            min=0.0,
            max=1000.0,
            step=0.01,
        ),
    ],
    build=_build_tick_ma_breakout,
    strategy_class=TickMaBreakoutStrategy,
)
