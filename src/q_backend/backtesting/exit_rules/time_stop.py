from __future__ import annotations

from typing import Any

from q_backend.backtesting.candle_kernel import enabled_rule_ids
from q_backend.backtesting.exit_rules.base import NEXT_OPEN_FILL_SENTENCE, ExitRule
from q_backend.backtesting.strategy_registry import StrategyParamSpec


class TimeStopRule(ExitRule):
    id = "time_stop"
    exit_group = "time"
    label = "Time Stop"
    description = (
        "Triggers once the number of completed bars in the trade reaches the maximum. " f"{NEXT_OPEN_FILL_SENTENCE}"
    )
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
        return self.id in enabled_rule_ids(params)
