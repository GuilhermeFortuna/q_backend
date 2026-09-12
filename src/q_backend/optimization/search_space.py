from typing import Any

import optuna

from q_backend.backtesting.entry_models import EntryInstance, EntryManagerConfig
from q_backend.backtesting.position_sizing import (
    FixedQuantityPositionSizing,
    FixedSafetyMarginPositionSizing,
    PositionSizingConfig,
)
from q_backend.optimization.models import (
    CategoricalParam,
    FloatParam,
    IntParam,
    LogFloatParam,
    SearchParam,
    SearchSpaceConfig,
    TrialParams,
)


def _suggest_param(trial: optuna.Trial, name: str, spec: SearchParam) -> str | int | float:
    if isinstance(spec, IntParam):
        return trial.suggest_int(name, spec.low, spec.high, step=spec.step)
    if isinstance(spec, FloatParam):
        if spec.step is not None:
            return trial.suggest_float(name, spec.low, spec.high, step=spec.step)
        return trial.suggest_float(name, spec.low, spec.high)
    if isinstance(spec, LogFloatParam):
        return trial.suggest_float(name, spec.low, spec.high, log=True)
    if isinstance(spec, CategoricalParam):
        return trial.suggest_categorical(name, spec.choices)
    raise ValueError(f"Unsupported search param type: {spec.type}")


def suggest_params(trial: optuna.Trial, search_space: SearchSpaceConfig) -> TrialParams:
    strategy_params: dict[str, Any] = {}
    risk_params: dict[str, Any] = {}
    manager_params: dict[str, Any] = {}

    for key, spec in search_space.strategy_params.items():
        strategy_params[key] = _suggest_param(trial, f"strategy__{key}", spec)

    for key, spec in search_space.risk_params.items():
        risk_params[key] = _suggest_param(trial, f"risk__{key}", spec)

    for key, spec in search_space.manager_params.items():
        manager_params[key] = _suggest_param(trial, f"manager__{key}", spec)

    return TrialParams(
        strategy_params=strategy_params,
        risk_params=risk_params,
        manager_params=manager_params,
    )


def entries_from_trial_params(
    strategy_params: dict[str, Any],
    entries_template: list[EntryInstance],
    *,
    fixed_strategy_params: dict[str, Any] | None = None,
) -> list[EntryInstance]:
    merged = {**(fixed_strategy_params or {}), **strategy_params}
    instances: list[EntryInstance] = []

    for index, template in enumerate(entries_template):
        slot_id = f"e{index}"
        prefix = f"{slot_id}__"
        params = dict(template.params)
        for key, value in merged.items():
            if key.startswith(prefix):
                params[key[len(prefix) :]] = value
        instances.append(EntryInstance(strategy=template.strategy, params=params))

    return instances


def manager_params_from_trial(
    manager_params: dict[str, Any],
    manager_template: EntryManagerConfig,
    *,
    fixed_params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    resolved = dict(manager_template.params)
    if fixed_params:
        for key, value in fixed_params.items():
            if key.startswith("manager__"):
                resolved[key[len("manager__") :]] = value
    resolved.update(manager_params)
    return resolved


def build_position_sizing_config(
    risk_params: dict[str, Any],
) -> PositionSizingConfig | None:
    sizing_type = risk_params.get("type")
    if sizing_type is None:
        return None

    if sizing_type == "fixed_quantity":
        return FixedQuantityPositionSizing(
            type="fixed_quantity",
            quantity=float(risk_params.get("quantity", 1.0)),
        )

    if sizing_type == "fixed_safety_margin":
        max_contracts = risk_params.get("max_contracts")
        return FixedSafetyMarginPositionSizing(
            type="fixed_safety_margin",
            safety_margin_per_contract=float(risk_params.get("safety_margin_per_contract", 5000.0)),
            min_contracts=int(risk_params.get("min_contracts", 1)),
            max_contracts=int(max_contracts) if max_contracts is not None else None,
        )

    raise ValueError(f"Unknown position sizing type: {sizing_type}")
