from __future__ import annotations

from typing import Any

from q_backend.backtesting.candle_kernel import enabled_rule_ids
from q_backend.backtesting.exit_rules.base import ExitRule
from q_backend.backtesting.strategy_registry import StrategyParamSpec


class FixedStopLossRule(ExitRule):
    id = "fixed_sl"
    exit_group = "stop_loss"
    label = "Fixed Stop Loss"
    description = "Exit when price moves against the position by a fixed percentage from entry."
    enable_param = "stop_loss_pct"
    enable_value = 0.02

    def param_specs(self) -> list[StrategyParamSpec]:
        return [
            StrategyParamSpec(
                name="stop_loss_pct",
                label="Stop Loss (%)",
                type="float",
                default=0.0,
                min=0.0,
                max=0.50,
                step=0.001,
                search_min=0.002,
                search_max=0.05,
                search_scale="log",
                hint="Fixed stop loss percentage from entry price (e.g. 0.02 = 2%). 0.0 to disable.",
                exit_group="stop_loss",
            ),
        ]

    def is_enabled(self, params: dict[str, Any]) -> bool:
        return self.id in enabled_rule_ids(params)


class AtrStopLossRule(ExitRule):
    id = "atr_sl"
    exit_group = "stop_loss"
    label = "ATR Stop Loss"
    description = "Exit when price breaches entry minus or plus an ATR multiple."
    enable_param = "stop_loss_atr"
    enable_value = 2.0

    def required_param_names(self) -> list[str]:
        return ["atr_period"]

    def param_specs(self) -> list[StrategyParamSpec]:
        return [
            StrategyParamSpec(
                name="stop_loss_atr",
                label="Stop Loss (ATR Mult)",
                type="float",
                default=0.0,
                min=0.0,
                max=10.0,
                step=0.1,
                search_min=1.0,
                search_max=4.0,
                search_step=0.5,
                hint="Stop loss as a multiple of ATR from entry price. 0.0 to disable.",
                exit_group="stop_loss",
            ),
            StrategyParamSpec(
                name="atr_period",
                label="ATR Period",
                type="int",
                default=14,
                min=2,
                max=100,
                step=1,
                search_min=7,
                search_max=28,
                search_step=7,
                hint="Period for ATR calculation used by ATR exits.",
                exit_group="general",
            ),
        ]

    def is_enabled(self, params: dict[str, Any]) -> bool:
        return self.id in enabled_rule_ids(params)

    def required_columns(self, params: dict[str, Any]) -> list[str]:
        if not self.is_enabled(params):
            return []
        period = int(params.get("atr_period", 14))
        return [f"atr_{period}"]


class FixedTakeProfitRule(ExitRule):
    id = "fixed_tp"
    exit_group = "target"
    label = "Fixed Take Profit"
    description = "Exit when price reaches a fixed percentage gain from entry."
    enable_param = "take_profit_pct"
    enable_value = 0.05

    def param_specs(self) -> list[StrategyParamSpec]:
        return [
            StrategyParamSpec(
                name="take_profit_pct",
                label="Take Profit (%)",
                type="float",
                default=0.0,
                min=0.0,
                max=1.0,
                step=0.001,
                search_min=0.003,
                search_max=0.10,
                search_scale="log",
                hint="Fixed take profit percentage from entry price (e.g. 0.05 = 5%). 0.0 to disable.",
                exit_group="target",
            ),
        ]

    def is_enabled(self, params: dict[str, Any]) -> bool:
        return self.id in enabled_rule_ids(params)


class AtrTakeProfitRule(ExitRule):
    id = "atr_tp"
    exit_group = "target"
    label = "ATR Take Profit"
    description = "Exit when price reaches entry plus or minus an ATR multiple in profit."
    enable_param = "take_profit_atr"
    enable_value = 3.0

    def required_param_names(self) -> list[str]:
        return ["atr_period"]

    def param_specs(self) -> list[StrategyParamSpec]:
        return [
            StrategyParamSpec(
                name="take_profit_atr",
                label="Take Profit (ATR Mult)",
                type="float",
                default=0.0,
                min=0.0,
                max=20.0,
                step=0.1,
                search_min=1.0,
                search_max=6.0,
                search_step=0.5,
                hint="Take profit as a multiple of ATR from entry price. 0.0 to disable.",
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


class TrailingStopRule(ExitRule):
    id = "trailing"
    exit_group = "trailing"
    label = "Percent Trailing Stop"
    description = "Exit when price retraces a fixed percentage from the in-trade peak or trough."
    enable_param = "trailing_stop_pct"
    enable_value = 0.02

    def param_specs(self) -> list[StrategyParamSpec]:
        return [
            StrategyParamSpec(
                name="trailing_stop_pct",
                label="Trailing Stop (%)",
                type="float",
                default=0.0,
                min=0.0,
                max=0.50,
                step=0.001,
                search_min=0.003,
                search_max=0.06,
                search_scale="log",
                hint="Trailing stop percentage from peak price (e.g. 0.02 = 2%). 0.0 to disable.",
                exit_group="trailing",
            ),
        ]

    def is_enabled(self, params: dict[str, Any]) -> bool:
        return self.id in enabled_rule_ids(params)


LEGACY_EXIT_RULES: list[ExitRule] = [
    FixedStopLossRule(),
    AtrStopLossRule(),
    FixedTakeProfitRule(),
    AtrTakeProfitRule(),
    TrailingStopRule(),
]
