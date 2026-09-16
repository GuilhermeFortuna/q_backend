from __future__ import annotations

from typing import Any

from q_backend.backtesting.candle_kernel import enabled_rule_ids
from q_backend.backtesting.exit_rules.base import ExitRule
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
        return self.id in enabled_rule_ids(params)

    def required_columns(self, params: dict[str, Any]) -> list[str]:
        if not self.is_enabled(params):
            return []
        period = int(params.get("atr_period", 14))
        return [f"atr_{period}"]
