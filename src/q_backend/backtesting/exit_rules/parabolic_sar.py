from __future__ import annotations

from typing import Any

from q_backend.backtesting.candle_kernel import enabled_rule_ids
from q_backend.backtesting.exit_rules.base import ExitRule
from q_backend.backtesting.strategy_registry import StrategyParamSpec


class ParabolicSarStopRule(ExitRule):
    id = "psar"
    exit_group = "trailing"
    label = "Parabolic SAR Trailing Stop"
    description = "Textbook Wilder parabolic SAR trailing stop updated each bar in rule state."
    enable_param = "psar_af_start"
    enable_value = 0.02

    def param_specs(self) -> list[StrategyParamSpec]:
        return [
            StrategyParamSpec(
                name="psar_af_start",
                label="Parabolic SAR AF Start",
                type="float",
                default=0.0,
                min=0.0,
                max=0.10,
                step=0.005,
                hint="Initial acceleration factor for Parabolic SAR trailing stop. 0.0 to disable.",
                exit_group="trailing",
            ),
            StrategyParamSpec(
                name="psar_af_step",
                label="Parabolic SAR AF Step",
                type="float",
                default=0.02,
                min=0.0,
                max=0.10,
                step=0.005,
                hint="AF increment when a new extreme point is made.",
                exit_group="trailing",
            ),
            StrategyParamSpec(
                name="psar_af_max",
                label="Parabolic SAR AF Max",
                type="float",
                default=0.2,
                min=0.0,
                max=1.0,
                step=0.01,
                hint="Maximum acceleration factor for Parabolic SAR.",
                exit_group="trailing",
            ),
        ]

    def is_enabled(self, params: dict[str, Any]) -> bool:
        return self.id in enabled_rule_ids(params)
