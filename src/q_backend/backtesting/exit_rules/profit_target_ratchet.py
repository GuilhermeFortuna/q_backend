from __future__ import annotations

from typing import Any

from q_backend.backtesting.candle_kernel import enabled_rule_ids
from q_backend.backtesting.exit_rules.base import ExitRule
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
        return self.id in enabled_rule_ids(params)

    def required_columns(self, params: dict[str, Any]) -> list[str]:
        if not self.is_enabled(params):
            return []
        period = int(params.get("atr_period", 14))
        return [f"atr_{period}"]
