from __future__ import annotations

from typing import Any

import pandas as pd

from q_backend.backtesting.exit_rules.base import ExitRule
from q_backend.backtesting.models import SignalAction, Trade
from q_backend.backtesting.strategy_registry import StrategyParamSpec


def _is_long(trade: Trade) -> bool:
    return trade.action == SignalAction.BUY or trade.action == "BUY"


def _bar_prices(data: pd.Series) -> tuple[float, float, float]:
    current_close = data.get("close", 0.0)
    current_high = data.get("high", current_close)
    current_low = data.get("low", current_close)
    return current_close, current_high, current_low


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
                hint="Fixed stop loss percentage from entry price (e.g. 0.02 = 2%). 0.0 to disable.",
                exit_group="stop_loss",
            ),
        ]

    def is_enabled(self, params: dict[str, Any]) -> bool:
        return float(params.get("stop_loss_pct", 0.0)) > 0

    def should_exit(
        self,
        trade: Trade,
        data: pd.Series,
        state: dict[str, Any],
        params: dict[str, Any],
    ) -> bool:
        stop_loss_pct = float(params.get("stop_loss_pct", 0.0))
        _, current_high, current_low = _bar_prices(data)

        if _is_long(trade):
            sl_price = trade.entry_price * (1.0 - stop_loss_pct)
            return current_low <= sl_price

        sl_price = trade.entry_price * (1.0 + stop_loss_pct)
        return current_high >= sl_price


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
                hint="Period for ATR calculation used by ATR exits.",
                exit_group="general",
            ),
        ]

    def is_enabled(self, params: dict[str, Any]) -> bool:
        return float(params.get("stop_loss_atr", 0.0)) > 0

    def required_columns(self, params: dict[str, Any]) -> list[str]:
        if not self.is_enabled(params):
            return []
        period = int(params.get("atr_period", 14))
        return [f"atr_{period}"]

    def should_exit(
        self,
        trade: Trade,
        data: pd.Series,
        state: dict[str, Any],
        params: dict[str, Any],
    ) -> bool:
        stop_loss_atr = float(params.get("stop_loss_atr", 0.0))
        period = int(params.get("atr_period", 14))
        atr_col = f"atr_{period}"
        atr_val = data.get(atr_col, None)
        if atr_val is None or pd.isna(atr_val):
            return False

        _, current_high, current_low = _bar_prices(data)

        if _is_long(trade):
            sl_price = trade.entry_price - (stop_loss_atr * atr_val)
            return current_low <= sl_price

        sl_price = trade.entry_price + (stop_loss_atr * atr_val)
        return current_high >= sl_price


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
                hint="Fixed take profit percentage from entry price (e.g. 0.05 = 5%). 0.0 to disable.",
                exit_group="target",
            ),
        ]

    def is_enabled(self, params: dict[str, Any]) -> bool:
        return float(params.get("take_profit_pct", 0.0)) > 0

    def should_exit(
        self,
        trade: Trade,
        data: pd.Series,
        state: dict[str, Any],
        params: dict[str, Any],
    ) -> bool:
        take_profit_pct = float(params.get("take_profit_pct", 0.0))
        _, current_high, current_low = _bar_prices(data)

        if _is_long(trade):
            tp_price = trade.entry_price * (1.0 + take_profit_pct)
            return current_high >= tp_price

        tp_price = trade.entry_price * (1.0 - take_profit_pct)
        return current_low <= tp_price


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
                hint="Take profit as a multiple of ATR from entry price. 0.0 to disable.",
                exit_group="target",
            ),
        ]

    def is_enabled(self, params: dict[str, Any]) -> bool:
        return float(params.get("take_profit_atr", 0.0)) > 0

    def required_columns(self, params: dict[str, Any]) -> list[str]:
        if not self.is_enabled(params):
            return []
        period = int(params.get("atr_period", 14))
        return [f"atr_{period}"]

    def should_exit(
        self,
        trade: Trade,
        data: pd.Series,
        state: dict[str, Any],
        params: dict[str, Any],
    ) -> bool:
        take_profit_atr = float(params.get("take_profit_atr", 0.0))
        period = int(params.get("atr_period", 14))
        atr_col = f"atr_{period}"
        atr_val = data.get(atr_col, None)
        if atr_val is None or pd.isna(atr_val):
            return False

        _, current_high, current_low = _bar_prices(data)

        if _is_long(trade):
            tp_price = trade.entry_price + (take_profit_atr * atr_val)
            return current_high >= tp_price

        tp_price = trade.entry_price - (take_profit_atr * atr_val)
        return current_low <= tp_price


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
                hint="Trailing stop percentage from peak price (e.g. 0.02 = 2%). 0.0 to disable.",
                exit_group="trailing",
            ),
        ]

    def is_enabled(self, params: dict[str, Any]) -> bool:
        return float(params.get("trailing_stop_pct", 0.0)) > 0

    def on_bar(
        self,
        trade: Trade,
        data: pd.Series,
        state: dict[str, Any],
        params: dict[str, Any],
    ) -> None:
        _, current_high, current_low = _bar_prices(data)

        if "extreme" not in state:
            if _is_long(trade):
                state["extreme"] = max(trade.entry_price, current_high)
            else:
                state["extreme"] = min(trade.entry_price, current_low)
        elif _is_long(trade):
            state["extreme"] = max(state["extreme"], current_high)
        else:
            state["extreme"] = min(state["extreme"], current_low)

    def should_exit(
        self,
        trade: Trade,
        data: pd.Series,
        state: dict[str, Any],
        params: dict[str, Any],
    ) -> bool:
        trailing_stop_pct = float(params.get("trailing_stop_pct", 0.0))
        _, current_high, current_low = _bar_prices(data)

        if _is_long(trade):
            highest_seen = state.get("extreme", trade.entry_price)
            trail_price = highest_seen * (1.0 - trailing_stop_pct)
            return current_low <= trail_price

        lowest_seen = state.get("extreme", trade.entry_price)
        trail_price = lowest_seen * (1.0 + trailing_stop_pct)
        return current_high >= trail_price


LEGACY_EXIT_RULES: list[ExitRule] = [
    FixedStopLossRule(),
    AtrStopLossRule(),
    FixedTakeProfitRule(),
    AtrTakeProfitRule(),
    TrailingStopRule(),
]
