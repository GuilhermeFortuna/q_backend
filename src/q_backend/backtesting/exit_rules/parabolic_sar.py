from __future__ import annotations

from typing import Any

import pandas as pd

from q_backend.backtesting.exit_rules.base import ExitRule
from q_backend.backtesting.exit_rules.legacy import _bar_prices, _is_long
from q_backend.backtesting.models import Trade
from q_backend.backtesting.strategy_registry import StrategyParamSpec


def _clamp_psar_long(
    sar: float,
    prior_low: float | None,
    prior_prior_low: float | None,
) -> float:
    clamped = sar
    if prior_low is not None:
        clamped = min(clamped, prior_low)
    if prior_prior_low is not None:
        clamped = min(clamped, prior_prior_low)
    return clamped


def _clamp_psar_short(
    sar: float,
    prior_high: float | None,
    prior_prior_high: float | None,
) -> float:
    clamped = sar
    if prior_high is not None:
        clamped = max(clamped, prior_high)
    if prior_prior_high is not None:
        clamped = max(clamped, prior_prior_high)
    return clamped


def update_psar_long(
    state: dict[str, Any],
    high: float,
    low: float,
    af_start: float,
    af_step: float,
    af_max: float,
    entry_price: float,
) -> None:
    if "sar" not in state:
        state["sar"] = min(entry_price, low)
        state["ep"] = max(entry_price, high)
        state["af"] = af_start
        state["prior_low"] = low
        state["prior_prior_low"] = None
        return

    state["sar"] = state["sar"] + state["af"] * (state["ep"] - state["sar"])
    state["sar"] = _clamp_psar_long(
        state["sar"],
        state.get("prior_low"),
        state.get("prior_prior_low"),
    )

    if high > state["ep"]:
        state["ep"] = high
        state["af"] = min(state["af"] + af_step, af_max)

    state["prior_prior_low"] = state.get("prior_low")
    state["prior_low"] = low


def update_psar_short(
    state: dict[str, Any],
    high: float,
    low: float,
    af_start: float,
    af_step: float,
    af_max: float,
    entry_price: float,
) -> None:
    if "sar" not in state:
        state["sar"] = max(entry_price, high)
        state["ep"] = min(entry_price, low)
        state["af"] = af_start
        state["prior_high"] = high
        state["prior_prior_high"] = None
        return

    state["sar"] = state["sar"] + state["af"] * (state["ep"] - state["sar"])
    state["sar"] = _clamp_psar_short(
        state["sar"],
        state.get("prior_high"),
        state.get("prior_prior_high"),
    )

    if low < state["ep"]:
        state["ep"] = low
        state["af"] = min(state["af"] + af_step, af_max)

    state["prior_prior_high"] = state.get("prior_high")
    state["prior_high"] = high


class ParabolicSarStopRule(ExitRule):
    id = "psar"
    exit_group = "trailing"
    label = "Parabolic SAR Trailing Stop"
    description = "Textbook Wilder parabolic SAR trailing stop updated each bar in rule state."
    enable_param = "psar_af_start"

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
        return float(params.get("psar_af_start", 0.0)) > 0

    def on_bar(
        self,
        trade: Trade,
        data: pd.Series,
        state: dict[str, Any],
        params: dict[str, Any],
    ) -> None:
        _, current_high, current_low = _bar_prices(data)
        af_start = float(params.get("psar_af_start", 0.0))
        af_step = float(params.get("psar_af_step", 0.02))
        af_max = float(params.get("psar_af_max", 0.2))

        if _is_long(trade):
            update_psar_long(
                state,
                current_high,
                current_low,
                af_start,
                af_step,
                af_max,
                trade.entry_price,
            )
        else:
            update_psar_short(
                state,
                current_high,
                current_low,
                af_start,
                af_step,
                af_max,
                trade.entry_price,
            )

    def should_exit(
        self,
        trade: Trade,
        data: pd.Series,
        state: dict[str, Any],
        params: dict[str, Any],
    ) -> bool:
        sar = state.get("sar")
        if sar is None:
            return False

        _, current_high, current_low = _bar_prices(data)

        if _is_long(trade):
            return current_low <= sar

        return current_high >= sar
