"""Derive WO30-shaped search spaces from genome param references."""

from __future__ import annotations

from typing import Any

from q_backend.backtesting.genome.exit_rule_policy import exit_policy_enable_param_names
from q_backend.backtesting.genome.param_bounds import GENOME_PARAM_BOUNDS
from q_backend.backtesting.genome.schema import Genome
from q_backend.backtesting.genome.validate import collect_genome_param_keys
from q_backend.backtesting.strategy_registry import StrategyParamSpec
from q_backend.optimization.models import (
    CategoricalParam,
    FloatParam,
    IntParam,
    SearchParam,
    SearchSpaceConfig,
)


def _search_param_from_spec(spec: StrategyParamSpec) -> SearchParam | None:
    if spec.type == "int":
        if spec.min is not None and spec.max is not None and spec.min < spec.max:
            step = int(spec.step) if spec.step else 1
            return IntParam(low=int(spec.min), high=int(spec.max), step=step)
        return None

    if spec.type == "float":
        if spec.min is not None and spec.max is not None and spec.min < spec.max:
            return FloatParam(low=spec.min, high=spec.max, step=spec.step)
        return None

    if spec.type == "categorical":
        if spec.choices is not None and len(spec.choices) > 1:
            return CategoricalParam(choices=list(spec.choices))
        return None

    return None


def _force_enable_param_includes_off(param: SearchParam) -> SearchParam:
    if isinstance(param, IntParam):
        return IntParam(low=0, high=param.high, step=param.step)
    if isinstance(param, FloatParam):
        return FloatParam(low=0.0, high=param.high, step=param.step)
    return param


def _exit_runtime_name_for_genome_key(genome_key: str) -> str | None:
    prefix = "exit_"
    if not genome_key.startswith(prefix):
        return None
    return genome_key.removeprefix(prefix)


def derive_genome_search_space(
    genome: Genome | dict[str, Any],
) -> tuple[SearchSpaceConfig, dict[str, Any]]:
    """Return ``(search_space, fixed_params)`` compatible with WO30 / WO31.

    Each ``{"param": key}`` slot in the genome becomes an Optuna dimension with
    bounds from ``GENOME_PARAM_BOUNDS``. Literal values remain inside the genome
    JSON and are not duplicated in ``fixed_params``.
    """
    if isinstance(genome, dict):
        genome = Genome.model_validate(genome)

    enable_runtime_names = exit_policy_enable_param_names(genome)
    strategy_params: dict[str, SearchParam] = {}
    fixed_params: dict[str, Any] = {}

    for key in sorted(collect_genome_param_keys(genome)):
        spec = GENOME_PARAM_BOUNDS[key]
        search_param = _search_param_from_spec(spec)
        if search_param is not None:
            runtime_name = _exit_runtime_name_for_genome_key(key)
            if runtime_name is not None and runtime_name in enable_runtime_names:
                search_param = _force_enable_param_includes_off(search_param)
            strategy_params[key] = search_param
        else:
            fixed_params[key] = spec.default

    return (
        SearchSpaceConfig(strategy_params=strategy_params, risk_params={}),
        fixed_params,
    )
