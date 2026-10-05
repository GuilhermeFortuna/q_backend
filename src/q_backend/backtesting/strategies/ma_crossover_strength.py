"""MA crossover with a causal volatility-normalized entry gate."""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd

from q_backend.backtesting.signal_columns import SIGNAL_ENTRY, SIGNAL_STRENGTH
from q_backend.backtesting.strategy import MACrossoverStrategy, ChartIndicatorSpec
from q_backend.backtesting.strategy_registry import StrategyParamSpec, register_strategy
from q_backend.backtesting.strategies.ma_crossover import ma_crossover_param_specs
from q_backend.backtesting.technical_indicators import compute_atr


class MACrossoverStrengthStrategy(MACrossoverStrategy):
    """Keep crossover exits even when the inverse entry is too weak.

    Raw buy/sell edges remain available for stance derivation. Composite research
    runs reapply the gate to final entries after combining those raw stances.
    """

    requires_single_entry = True

    def __init__(self, atr_period: int = 14, min_cross_strength: float = 0.1616, **kwargs: Any):
        if not isinstance(atr_period, int) or isinstance(atr_period, bool) or atr_period < 1:
            raise ValueError("atr_period must be a positive integer")
        if not math.isfinite(min_cross_strength) or min_cross_strength < 0:
            raise ValueError("min_cross_strength must be finite and nonnegative")
        self.atr_period = atr_period
        self.min_cross_strength = float(min_cross_strength)
        defaults = dict(short_period=9, long_period=20, short_ma_type="ema", long_ma_type="wma")
        super().__init__(**{**defaults, **kwargs})
        self.parameters.update(atr_period=atr_period, min_cross_strength=self.min_cross_strength)

    def compute_indicators(self, data: pd.DataFrame) -> pd.DataFrame:
        frame = super().compute_indicators(data)
        frame["atr"] = compute_atr(frame.high, frame.low, frame.close, self.atr_period)
        return self.filter_entry_signals(frame)

    def filter_entry_signals(self, frame: pd.DataFrame, *, indicator_prefix: str = "") -> pd.DataFrame:
        entry = frame[SIGNAL_ENTRY]
        atr = frame[f"{indicator_prefix}atr"]
        change = frame[f"{indicator_prefix}delta"] - frame[f"{indicator_prefix}prev_delta"]
        score = entry * change / atr.where(atr > 0)
        accepted = entry.ne(0) & np.isfinite(score) & score.gt(self.min_cross_strength)
        frame[f"{indicator_prefix}crossover_strength"] = score.where(entry.ne(0))
        frame[f"{indicator_prefix}strength_filter_accepted"] = accepted
        frame[SIGNAL_ENTRY] = entry.where(accepted, 0).astype(np.int8)
        frame[SIGNAL_STRENGTH] = frame[SIGNAL_STRENGTH].where(accepted, 0.0).astype(np.float64)
        return frame

    def get_chart_indicators(self) -> list[ChartIndicatorSpec]:
        return [
            *super().get_chart_indicators(),
            ChartIndicatorSpec(key="crossover_strength", label="Crossover strength / ATR", pane="oscillator"),
        ]


def _build(params: dict[str, Any], symbol: str) -> MACrossoverStrengthStrategy:
    return MACrossoverStrengthStrategy(
        short_period=int(params["short_period"]),
        long_period=int(params["long_period"]),
        short_ma_type=params["short_ma_type"],
        long_ma_type=params["long_ma_type"],
        threshold=float(params["threshold"]),
        atr_period=int(params["atr_period"]),
        min_cross_strength=float(params["min_cross_strength"]),
        symbol=symbol,
    )


params = ma_crossover_param_specs()
for param in params:
    if param.name in {"short_period", "long_period", "short_ma_type", "long_ma_type"}:
        param.default = {"short_period": 9, "long_period": 20, "short_ma_type": "ema", "long_ma_type": "wma"}[
            param.name
        ]
# Search ranges should include the strategy's defaults.
for param in params:
    if param.name == "short_period":
        param.search_min, param.search_max, param.search_step = 3, 30, 3
    elif param.name == "long_period":
        param.search_min, param.search_max, param.search_step = 10, 100, 10
params += [
    StrategyParamSpec(
        name="atr_period",
        label="ATR Period",
        type="int",
        default=14,
        min=1,
        max=200,
        step=1,
        search_min=7,
        search_max=28,
        search_step=7,
        hint="Closed-bar ATR window used to normalize crossover strength.",
    ),
    StrategyParamSpec(
        name="min_cross_strength",
        label="Minimum Crossover Strength",
        type="float",
        default=0.1616,
        min=0.0,
        max=5.0,
        step=0.0001,
        search_min=0.0,
        search_max=0.4,
        search_step=0.05,
        hint="Accept only strength strictly above this cutoff. The 0.1616 default is exploratory from CCM H1; exits remain active. Use as a single entry strategy.",
    ),
]
register_strategy(
    name="MACrossoverStrengthFilter",
    label="MA Crossover — Strength Filter",
    description="MA crossover entries gated by direction-adjusted change in MA separation / ATR; original crossover exits stay active. Single entry strategy.",
    params=params,
    build=_build,
    strategy_class=MACrossoverStrengthStrategy,
    category="trend",
    thesis="Select decisive crossovers relative to recent volatility while retaining opposite-cross exits.",
    strong_in="Decisive crossover entries; exploratory CCM H1 evidence, not validated across markets.",
    weak_in="Weak crossovers and changing regimes; filtering can miss large profitable moves.",
)
