"""Derive Discovery search spaces for exit-preset candidate expansion (WO79).

Each preset candidate searches entry params unchanged plus the preset's exit
family on/off and magnitude. Non-preset exit enable params are pinned off so
candidates stay interpretable.
"""

from __future__ import annotations

from typing import Any

from q_backend.backtesting.exit_rules.registry import all_param_specs, list_exit_rules
from q_backend.backtesting.strategy_registry import (
    ExitPreset,
    ExitRuleInfo,
    StrategyParamSpec,
    get_registered_strategy,
)
from q_backend.optimization.auto_search_space import (
    _search_param_from_spec,
    default_risk_search_space,
)
from q_backend.optimization.models import (
    FloatParam,
    IntParam,
    SearchParam,
    SearchSpaceConfig,
)


def _derive_entry_search_space(
    strategy_name: str,
) -> tuple[dict[str, SearchParam], dict[str, Any]]:
    entry = get_registered_strategy(strategy_name)
    strategy_params: dict[str, SearchParam] = {}
    fixed_params: dict[str, Any] = {}

    for spec in entry.info.params:
        if spec.exit_group is not None:
            continue
        search_param = _search_param_from_spec(spec)
        if search_param is not None:
            strategy_params[spec.name] = search_param
        else:
            fixed_params[spec.name] = spec.default

    return strategy_params, fixed_params


def _rules_for_preset(preset: ExitPreset) -> list[ExitRuleInfo]:
    preset_params = set(preset.parameters.keys())
    rules: list[ExitRuleInfo] = []
    for info in list_exit_rules():
        if info.enable_param in preset_params:
            rules.append(info)
            continue
        if any(name in preset_params for name in info.param_names):
            rules.append(info)
    return rules


def _force_enable_param_includes_off(param: SearchParam) -> SearchParam:
    if isinstance(param, IntParam):
        return IntParam(low=0, high=param.high, step=param.step)
    if isinstance(param, FloatParam):
        return FloatParam(low=0.0, high=param.high, step=param.step)
    return param


def _fallback_enable_search_param(spec: StrategyParamSpec) -> SearchParam | None:
    if spec.type == "int":
        high = int(spec.max) if spec.max is not None else 100
        return IntParam(low=0, high=max(high, 1), step=int(spec.step or 1))
    if spec.type == "float":
        high = float(spec.max) if spec.max is not None else 1.0
        return FloatParam(low=0.0, high=max(high, 0.001), step=spec.step)
    return None


def preset_exit_param_names(preset: ExitPreset) -> list[str]:
    """Exit parameter names searched for a preset candidate (excludes entry params)."""
    preset_rules = _rules_for_preset(preset)
    names: set[str] = set(preset.parameters.keys())
    for rule in preset_rules:
        names.add(rule.enable_param)
        names.update(rule.param_names)
        names.update(rule.required_param_names)
    return sorted(names)


def derive_exit_preset_search_space(
    strategy_name: str,
    preset: ExitPreset,
    *,
    include_risk: bool,
    pin_non_preset_exits_off: bool = True,
) -> tuple[SearchSpaceConfig, dict[str, Any]]:
    """Return ``(search_space, fixed_params)`` for one exit-preset candidate.

    Entry params are searchable as in the baseline registry sweep. Preset-owned
    exit params (including enable params with ``low == 0``) are searchable.
    Shared params such as ``atr_period`` are included when the preset needs them.
    Other applicable exit enable params are pinned to ``0`` when
    ``pin_non_preset_exits_off`` is true.
    """
    entry_params, fixed_params = _derive_entry_search_space(strategy_name)
    preset_rules = _rules_for_preset(preset)
    preset_param_names: set[str] = set(preset.parameters.keys())
    enable_params = {rule.enable_param for rule in preset_rules}

    for rule in preset_rules:
        preset_param_names.add(rule.enable_param)
        preset_param_names.update(rule.param_names)
        preset_param_names.update(rule.required_param_names)

    spec_by_name = {spec.name: spec for spec in all_param_specs()}
    exit_search_params: dict[str, SearchParam] = {}

    for name in sorted(preset_param_names):
        spec = spec_by_name.get(name)
        if spec is None:
            continue
        search_param = _search_param_from_spec(spec)
        if search_param is None:
            if name in enable_params:
                search_param = _fallback_enable_search_param(spec)
            if search_param is None:
                fixed_params[name] = spec.default
                continue
        if name in enable_params:
            search_param = _force_enable_param_includes_off(search_param)
        exit_search_params[name] = search_param

    if pin_non_preset_exits_off:
        for info in list_exit_rules():
            if info.enable_param not in preset_param_names:
                fixed_params[info.enable_param] = 0

    for spec in all_param_specs():
        if spec.name in entry_params or spec.name in exit_search_params:
            continue
        if spec.name in fixed_params:
            continue
        if spec.exit_group is None:
            continue
        fixed_params[spec.name] = spec.default

    strategy_params = {**entry_params, **exit_search_params}
    search_space = SearchSpaceConfig(strategy_params=strategy_params, risk_params={})
    if include_risk:
        risk_space = default_risk_search_space()
        search_space = SearchSpaceConfig(
            strategy_params=strategy_params,
            risk_params=risk_space.risk_params,
        )
    return search_space, fixed_params
