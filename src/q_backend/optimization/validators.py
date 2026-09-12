import optuna
from typing import Any

from q_backend.backtesting.entry_models import EntryInstance
from q_backend.backtesting.moving_averages import normalize_ma_type
from q_backend.optimization.models import TrialParams


def _validate_ma_crossover(strategy: dict[str, Any]) -> None:
    short_period = strategy.get("short_period")
    long_period = strategy.get("long_period")
    if short_period is not None and long_period is not None:
        if int(long_period) <= int(short_period):
            raise optuna.TrialPruned("long_period must be greater than short_period")

    for key in ("short_ma_type", "long_ma_type"):
        if key in strategy:
            try:
                normalize_ma_type(strategy[key])
            except ValueError as exc:
                raise optuna.TrialPruned(str(exc)) from exc


def _validate_risk_params(risk: dict[str, Any]) -> None:
    sizing_type = risk.get("type")
    if sizing_type == "fixed_safety_margin":
        min_contracts = risk.get("min_contracts")
        max_contracts = risk.get("max_contracts")
        if min_contracts is not None and max_contracts is not None and int(max_contracts) < int(min_contracts):
            raise optuna.TrialPruned("max_contracts must be >= min_contracts")


def validate_trial_params(
    trial_params: TrialParams,
    strategy_name: str,
    *,
    entries: list[EntryInstance] | None = None,
) -> None:
    strategy = trial_params.strategy_params
    risk = trial_params.risk_params

    if entries:
        for index, entry in enumerate(entries):
            prefix = f"e{index}__"
            instance_params = {key[len(prefix) :]: value for key, value in strategy.items() if key.startswith(prefix)}
            if entry.strategy == "MACrossover":
                _validate_ma_crossover(instance_params)
        _validate_risk_params(risk)
        return

    if strategy_name == "MACrossover":
        _validate_ma_crossover(strategy)

    _validate_risk_params(risk)
