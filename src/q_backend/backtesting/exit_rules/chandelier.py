from __future__ import annotations

from typing import Any

import pandas as pd

from q_backend.backtesting.exit_rules.base import ExitRule
from q_backend.backtesting.exit_rules.legacy import _bar_prices, _is_long
from q_backend.backtesting.models import Trade
from q_backend.backtesting.strategy_registry import StrategyParamSpec


class ChandelierExitRule(ExitRule):
    id = "chandelier"
    exit_group = "trailing"
    label = "Chandelier Exit"
    description = "Trailing stop at peak high minus an ATR multiple."
    enable_param = "chandelier_atr_mult"
    enable_value = 3.0

    def required_param_names(self) -> list[str]:
        return ["atr_period"]

    def param_specs(self) -> list[StrategyParamSpec]:
        return [
            StrategyParamSpec(
                name="chandelier_atr_mult",
                label="Chandelier (ATR Mult)",
                type="float",
                default=0.0,
                min=0.0,
                max=10.0,
                step=0.1,
                hint="Chandelier trailing stop: peak/trough minus/plus ATR multiple. 0.0 to disable.",
                exit_group="trailing",
            ),
        ]

    def is_enabled(self, params: dict[str, Any]) -> bool:
        return float(params.get("chandelier_atr_mult", 0.0)) > 0

    def required_columns(self, params: dict[str, Any]) -> list[str]:
        if not self.is_enabled(params):
            return []
        period = int(params.get("atr_period", 14))
        return [f"atr_{period}"]

    def on_bar(
        self,
        trade: Trade,
        data: pd.Series,
        state: dict[str, Any],
        params: dict[str, Any],
    ) -> None:
        _, current_high, current_low = _bar_prices(data)

        if "peak" not in state:
            state["peak"] = trade.entry_price
        if _is_long(trade):
            state["peak"] = max(state["peak"], current_high)
        else:
            state["peak"] = min(state["peak"], current_low)

    def should_exit(
        self,
        trade: Trade,
        data: pd.Series,
        state: dict[str, Any],
        params: dict[str, Any],
    ) -> bool:
        mult = float(params.get("chandelier_atr_mult", 0.0))
        period = int(params.get("atr_period", 14))
        atr_col = f"atr_{period}"
        atr_val = data.get(atr_col, None)
        if atr_val is None or pd.isna(atr_val):
            return False

        _, current_high, current_low = _bar_prices(data)
        peak = state.get("peak", trade.entry_price)

        if _is_long(trade):
            stop = peak - (mult * atr_val)
            return current_low <= stop

        stop = peak + (mult * atr_val)
        return current_high >= stop
