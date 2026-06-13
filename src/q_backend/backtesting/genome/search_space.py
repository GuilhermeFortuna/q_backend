"""Derive WO30-shaped search spaces from genome param references."""

from __future__ import annotations

from typing import Any

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

    strategy_params: dict[str, SearchParam] = {}
    fixed_params: dict[str, Any] = {}

    for key in sorted(collect_genome_param_keys(genome)):
        spec = GENOME_PARAM_BOUNDS[key]
        search_param = _search_param_from_spec(spec)
        if search_param is not None:
            strategy_params[key] = search_param
        else:
            fixed_params[key] = spec.default

    return (
        SearchSpaceConfig(strategy_params=strategy_params, risk_params={}),
        fixed_params,
    )
