from __future__ import annotations

from typing import Any

import pandas as pd

from q_backend.backtesting.exit_rules.base import ExitRule
from q_backend.backtesting.exit_rules.legacy import _bar_prices, _is_long
from q_backend.backtesting.models import Trade
from q_backend.backtesting.strategy_registry import StrategyParamSpec


class ProfitTargetRatchetRule(ExitRule):
    id = "profit_target_ratchet"
    exit_group = "target"
    label = "Profit Target Ratchet"
    description = "Arms a trailing profit floor once price reaches entry plus or minus an ATR multiple."
    enable_param = "target_ratchet_atr"
    enable_value = 2.0

    def required_param_names(self) -> list[str]:
        return ["atr_period"]

    def param_specs(self) -> list[StrategyParamSpec]:
        return [
            StrategyParamSpec(
                name="target_ratchet_atr",
                label="Profit Target Ratchet (ATR Mult)",
                type="float",
                default=0.0,
                min=0.0,
                max=20.0,
                step=0.1,
                hint=(
                    "Arms a trailing profit ratchet once price reaches entry ± ATR multiple; "
                    "ratchet trails with new extremes. 0.0 to disable."
                ),
                exit_group="target",
            ),
        ]

    def is_enabled(self, params: dict[str, Any]) -> bool:
        return float(params.get("target_ratchet_atr", 0.0)) > 0

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
        mult = float(params.get("target_ratchet_atr", 0.0))
        period = int(params.get("atr_period", 14))
        atr_col = f"atr_{period}"
        atr_val = data.get(atr_col, None)
        if atr_val is None or pd.isna(atr_val):
            return

        _, current_high, current_low = _bar_prices(data)

        if _is_long(trade):
            arm_level = trade.entry_price + (mult * atr_val)
            if not state.get("armed") and current_high >= arm_level:
                state["armed"] = True
                state["ratchet"] = current_high - (mult * atr_val)
            elif state.get("armed"):
                state["ratchet"] = max(
                    state["ratchet"],
                    current_high - (mult * atr_val),
                )
        else:
            arm_level = trade.entry_price - (mult * atr_val)
            if not state.get("armed") and current_low <= arm_level:
                state["armed"] = True
                state["ratchet"] = current_low + (mult * atr_val)
            elif state.get("armed"):
                state["ratchet"] = min(
                    state["ratchet"],
                    current_low + (mult * atr_val),
                )

    def should_exit(
        self,
        trade: Trade,
        data: pd.Series,
        state: dict[str, Any],
        params: dict[str, Any],
    ) -> bool:
        if not state.get("armed"):
            return False

        _, current_high, current_low = _bar_prices(data)
        ratchet = state["ratchet"]

        if _is_long(trade):
            return current_low <= ratchet

        return current_high >= ratchet
