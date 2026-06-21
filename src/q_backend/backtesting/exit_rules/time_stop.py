from __future__ import annotations

from typing import Any

import pandas as pd

from q_backend.backtesting.exit_rules.base import ExitRule
from q_backend.backtesting.models import Trade
from q_backend.backtesting.strategy_registry import StrategyParamSpec


class TimeStopRule(ExitRule):
    id = "time_stop"
    exit_group = "time"
    label = "Time Stop"
    description = "Close the position after a maximum number of bars in trade."
    enable_param = "max_bars_in_trade"
    enable_value = 50

    def param_specs(self) -> list[StrategyParamSpec]:
        return [
            StrategyParamSpec(
                name="max_bars_in_trade",
                label="Max Bars In Trade",
                type="int",
                default=0,
                min=0,
                max=5000,
                step=1,
                hint="Close the position after this many bars in trade. 0 to disable.",
                exit_group="time",
            ),
        ]

    def is_enabled(self, params: dict[str, Any]) -> bool:
        return int(params.get("max_bars_in_trade", 0)) > 0

    def on_bar(
        self,
        trade: Trade,
        data: pd.Series,
        state: dict[str, Any],
        params: dict[str, Any],
    ) -> None:
        state["bars"] = state.get("bars", 0) + 1

    def should_exit(
        self,
        trade: Trade,
        data: pd.Series,
        state: dict[str, Any],
        params: dict[str, Any],
    ) -> bool:
        max_bars = int(params.get("max_bars_in_trade", 0))
        return state.get("bars", 0) >= max_bars
