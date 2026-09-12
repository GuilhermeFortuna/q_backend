from __future__ import annotations

from typing import Any

import pandas as pd

from q_backend.backtesting.exit_rules.base import ExitRule
from q_backend.backtesting.exit_rules.legacy import _bar_prices, _is_long
from q_backend.backtesting.models import Trade
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
        return int(params.get("donchian_exit_period", 0)) > 0

    def required_columns(self, params: dict[str, Any]) -> list[str]:
        if not self.is_enabled(params):
            return []
        period = int(params.get("donchian_exit_period", 0))
        return [f"donchian_high_{period}", f"donchian_low_{period}"]

    def should_exit(
        self,
        trade: Trade,
        data: pd.Series,
        state: dict[str, Any],
        params: dict[str, Any],
    ) -> bool:
        period = int(params.get("donchian_exit_period", 0))
        high_col = f"donchian_high_{period}"
        low_col = f"donchian_low_{period}"
        donchian_high = data.get(high_col, None)
        donchian_low = data.get(low_col, None)
        if donchian_high is None or donchian_low is None or pd.isna(donchian_high) or pd.isna(donchian_low):
            return False

        _, current_high, current_low = _bar_prices(data)

        if _is_long(trade):
            return current_low <= donchian_low

        return current_high >= donchian_high
