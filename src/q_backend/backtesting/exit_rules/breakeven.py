from __future__ import annotations

from typing import Any

import pandas as pd

from q_backend.backtesting.exit_rules.base import ExitRule
from q_backend.backtesting.exit_rules.legacy import _bar_prices, _is_long
from q_backend.backtesting.models import Trade
from q_backend.backtesting.strategy_registry import StrategyParamSpec


class BreakevenStopRule(ExitRule):
    id = "breakeven"
    exit_group = "stop_loss"
    label = "Break-even Stop"
    description = "Arm a stop at entry plus a small offset once gain reaches a trigger threshold."
    enable_param = "breakeven_trigger_pct"
    enable_value = 0.02

    def param_specs(self) -> list[StrategyParamSpec]:
        return [
            StrategyParamSpec(
                name="breakeven_trigger_pct",
                label="Break-even Trigger (%)",
                type="float",
                default=0.0,
                min=0.0,
                max=0.50,
                step=0.001,
                search_min=0.002,
                search_max=0.03,
                search_scale="log",
                hint="Arm break-even stop once gain from entry reaches this fraction. 0.0 to disable.",
                exit_group="stop_loss",
            ),
            StrategyParamSpec(
                name="breakeven_offset_pct",
                label="Break-even Offset (%)",
                type="float",
                default=0.0,
                min=0.0,
                max=0.10,
                step=0.001,
                search_min=0.0005,
                search_max=0.005,
                search_scale="log",
                hint="Stop sits at entry +/- this fraction once armed (locks a sliver of profit).",
                exit_group="stop_loss",
            ),
        ]

    def is_enabled(self, params: dict[str, Any]) -> bool:
        return float(params.get("breakeven_trigger_pct", 0.0)) > 0

    def on_bar(
        self,
        trade: Trade,
        data: pd.Series,
        state: dict[str, Any],
        params: dict[str, Any],
    ) -> None:
        if state.get("armed"):
            return

        trigger = float(params.get("breakeven_trigger_pct", 0.0))
        _, current_high, current_low = _bar_prices(data)

        if _is_long(trade):
            gain = (current_high - trade.entry_price) / trade.entry_price
        else:
            gain = (trade.entry_price - current_low) / trade.entry_price

        if gain >= trigger:
            state["armed"] = True

    def should_exit(
        self,
        trade: Trade,
        data: pd.Series,
        state: dict[str, Any],
        params: dict[str, Any],
    ) -> bool:
        if not state.get("armed"):
            return False

        offset = float(params.get("breakeven_offset_pct", 0.0))
        _, current_high, current_low = _bar_prices(data)

        if _is_long(trade):
            stop = trade.entry_price * (1.0 + offset)
            return current_low <= stop

        stop = trade.entry_price * (1.0 - offset)
        return current_high >= stop
