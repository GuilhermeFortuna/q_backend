from __future__ import annotations

from typing import Any

from q_backend.backtesting.candle_kernel import enabled_rule_ids
from q_backend.backtesting.exit_rules.base import ExitRule
from q_backend.backtesting.strategy_registry import StrategyParamSpec


class DonchianChannelStopRule(ExitRule):
    id = "donchian_stop"
    exit_group = "trailing"
    label = "Donchian Channel Stop"
    description = "Exit when price crosses the opposite N-bar Donchian extreme."
    enable_param = "donchian_exit_period"
    enable_value = 20

    def param_specs(self) -> list[StrategyParamSpec]:
        return [
            StrategyParamSpec(
                name="donchian_exit_period",
                label="Donchian Exit Period",
                type="int",
                default=0,
                min=0,
                max=500,
                step=1,
                hint=("Exit when price crosses the opposite N-bar Donchian extreme " "(channel trail). 0 to disable."),
                exit_group="trailing",
            ),
        ]

    def is_enabled(self, params: dict[str, Any]) -> bool:
        return self.id in enabled_rule_ids(params)

    def required_columns(self, params: dict[str, Any]) -> list[str]:
        if not self.is_enabled(params):
            return []
        period = int(params.get("donchian_exit_period", 0))
        return [f"donchian_high_{period}", f"donchian_low_{period}"]
