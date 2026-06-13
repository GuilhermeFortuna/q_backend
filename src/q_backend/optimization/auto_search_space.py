"""Derive Optuna search spaces from strategy registry parameter specs.

Each registered strategy declares its parameters with optional bounds
(``min``/``max``/``step``/``choices``). This module is the bridge to
``SearchSpaceConfig``: bounded params become searchable dimensions; params
without usable bounds are pinned to their registry defaults via
``fixed_params`` (WO31 merges those into every trial).

``LogFloatParam`` is not inferred from registry metadata today — floats map
to ``FloatParam`` only. A future registry hint could enable log-scale search.
"""

from __future__ import annotations

from typing import Any

from q_backend.backtesting.strategy_registry import (
    StrategyParamSpec,
    get_registered_strategy,
)
from q_backend.optimization.models import (
    CategoricalParam,
    FloatParam,
    IntParam,
    LogFloatParam,
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


def derive_strategy_search_space(strategy_name: str) -> tuple[SearchSpaceConfig, dict[str, Any]]:
    """Return ``(search_space, fixed_params)``.

    ``search_space.strategy_params`` holds params that will be searched;
    ``fixed_params`` holds ``{name: default}`` for params kept constant
    during the study. ``risk_params`` is empty — use ``default_risk_search_space``
    or ``auto_search_space`` for position-sizing dimensions.
    """
    entry = get_registered_strategy(strategy_name)
    strategy_params: dict[str, SearchParam] = {}
    fixed_params: dict[str, Any] = {}

    for spec in entry.info.params:
        search_param = _search_param_from_spec(spec)
        if search_param is not None:
            strategy_params[spec.name] = search_param
        else:
            fixed_params[spec.name] = spec.default

    return (
        SearchSpaceConfig(strategy_params=strategy_params, risk_params={}),
        fixed_params,
    )


def default_risk_search_space() -> SearchSpaceConfig:
    """Conservative default position-sizing search space.

    Uses ``fixed_safety_margin`` sizing and searches ``safety_margin_per_contract``
    over a modest log-scale range. Keys match ``build_position_sizing_config`` in
    ``search_space.py`` (``type``, ``safety_margin_per_contract``).
    """
    return SearchSpaceConfig(
        strategy_params={},
        risk_params={
            # Pin sizing mode; WO31 may override or disable risk search entirely.
            "type": CategoricalParam(choices=["fixed_safety_margin"]),
            # Capital buffer per contract (log scale suits wide dollar ranges).
            "safety_margin_per_contract": LogFloatParam(low=1000.0, high=10000.0),
        },
    )


def auto_search_space(strategy_name: str, *, include_risk: bool = True) -> SearchSpaceConfig:
    """Build a complete ``SearchSpaceConfig`` from registry metadata.

    Strategy params come from ``derive_strategy_search_space``; when
    ``include_risk`` is true, ``default_risk_search_space`` risk params are
    merged in.

    For ``fixed_params`` (unbounded registry params pinned to defaults), call
    ``derive_strategy_search_space`` directly — WO31 merges them into trials.
    """
    search_space, _ = derive_strategy_search_space(strategy_name)
    if not include_risk:
        return search_space

    risk_space = default_risk_search_space()
    return SearchSpaceConfig(
        strategy_params=search_space.strategy_params,
        risk_params=risk_space.risk_params,
    )
