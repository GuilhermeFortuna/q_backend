from __future__ import annotations

from typing import Any

from q_backend.backtesting.exit_rules.base import ExitRule
from q_backend.backtesting.exit_rules.breakeven import BreakevenStopRule
from q_backend.backtesting.exit_rules.chandelier import ChandelierExitRule
from q_backend.backtesting.exit_rules.donchian_stop import DonchianChannelStopRule
from q_backend.backtesting.exit_rules.legacy import LEGACY_EXIT_RULES
from q_backend.backtesting.exit_rules.parabolic_sar import ParabolicSarStopRule
from q_backend.backtesting.exit_rules.profit_target_ratchet import ProfitTargetRatchetRule
from q_backend.backtesting.exit_rules.time_stop import TimeStopRule
from q_backend.backtesting.strategy_registry import StrategyParamSpec

SPECIALIZED_EXIT_RULES: list[ExitRule] = [
    ChandelierExitRule(),
    BreakevenStopRule(),
    ParabolicSarStopRule(),
    ProfitTargetRatchetRule(),
    TimeStopRule(),
    DonchianChannelStopRule(),
]

EXIT_RULES: list[ExitRule] = list(LEGACY_EXIT_RULES) + SPECIALIZED_EXIT_RULES


def enabled_rules(params: dict[str, Any]) -> list[ExitRule]:
    return [rule for rule in EXIT_RULES if rule.is_enabled(params)]


def all_param_specs() -> list[StrategyParamSpec]:
    seen: set[str] = set()
    specs: list[StrategyParamSpec] = []
    for rule in EXIT_RULES:
        for spec in rule.param_specs():
            if spec.name in seen:
                continue
            seen.add(spec.name)
            specs.append(spec)
    return specs


def required_columns(params: dict[str, Any]) -> list[str]:
    cols: set[str] = set()
    for rule in enabled_rules(params):
        cols.update(rule.required_columns(params))
    return sorted(cols)
